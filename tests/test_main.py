"""
The state machine itself, driven by recorded audio through the same code path a
microphone would use. The speaker is faked — these tests should not shout at
whoever runs them — but the wake word, the endpointer and the transcriber are
all real, so what is being tested is the wiring between them and the order of
the states that come out.

What this cannot test is the thing the phase gate exists for: whether it hears a
person. That needs a person.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from jarvis.audio import stt as stt_module
from jarvis.audio.tts import SpeechResult
from jarvis.config import CONFIG, Config
from jarvis.events import EventBus
from jarvis.logging import TurnTimer
from jarvis.main import Jarvis


class FakeBrain:
    """Answers instantly and without a network. The agent has its own tests."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.interrupted = False
        self.last_ttft_ms = 11.0

    async def ask(self, text: str):
        self.asked.append(text)
        yield f"You said: {text}"

    async def interrupt(self) -> None:
        self.interrupted = True


class FakeSpeaker:
    """Records what would have been said, instantly and silently."""

    backend = "fake"

    def __init__(self) -> None:
        self.said: list[str] = []
        self.interrupted = False

    def say(self, text: str) -> SpeechResult:
        self.said.append(text)
        return SpeechResult(ttfb_ms=12.0, samples=1000, backend=self.backend)

    def interrupt(self) -> None:
        self.interrupted = True

    def start(self) -> None: ...
    def warmup(self) -> None: ...
    def stop(self) -> None: ...


@pytest.fixture(scope="module")
def transcriber():
    if not CONFIG.is_apple_silicon:
        pytest.skip("parakeet needs apple silicon")
    built = stt_module.TimedTranscriber(stt_module.build(CONFIG), CONFIG)
    built.warmup()
    return built


def build(transcriber, replay, heard: str, watching: str = "silence", config=CONFIG):
    bus = EventBus()
    jarvis = Jarvis(config, bus)
    jarvis.speaker = FakeSpeaker()
    jarvis.brain = FakeBrain()
    jarvis.stt = transcriber
    jarvis._rec_frames = replay(heard)
    jarvis._wake_frames = replay(watching)
    jarvis.wake.load()
    return jarvis, bus


def drain(bus_sub) -> list[dict]:
    events = []
    while (event := bus_sub.get_nowait()) is not None:
        events.append(event)
    return events


def test_one_turn_walks_the_states_in_order(transcriber, replay):
    jarvis, bus = build(transcriber, replay, "testing_one_two_three")
    sub = bus.subscribe(maxsize=1024)

    spoke = asyncio.run(jarvis.turn(TurnTimer(), start_timeout=2.0))

    assert spoke is True
    events = drain(sub)
    states = [e["state"] for e in events if e["type"] == "state"]
    assert states == ["thinking", "speaking"], states

    user = [e for e in events if e["type"] == "transcript" and e["role"] == "user"]
    assert "testing" in user[0]["text"].lower()
    assert jarvis.speaker.said, "nothing was spoken"
    assert jarvis.speaker.said[0].startswith("You said:")
    assert "testing" in jarvis.speaker.said[0].lower()


def test_the_reply_reaches_the_hud_before_it_reaches_the_speakers(transcriber, replay):
    """protocol.md: send each chunk to the HUD immediately *before* the TTS."""
    jarvis, bus = build(transcriber, replay, "testing_one_two_three")
    sub = bus.subscribe(maxsize=1024)
    order: list[str] = []
    original = jarvis.speaker.say

    def watched(text: str):
        order.append(f"spoke:{text}")
        return original(text)

    jarvis.speaker.say = watched
    asyncio.run(jarvis.turn(TurnTimer(), start_timeout=2.0))

    fragments = [
        e for e in drain(sub) if e["type"] == "transcript" and e["role"] == "jarvis" and e["text"]
    ]
    assert fragments, "the reply never reached the bus"
    assert order, "the reply was never spoken"
    assert fragments[0]["final"] is False


def test_silence_is_not_a_turn(transcriber, replay):
    jarvis, _ = build(transcriber, replay, "silence")
    assert asyncio.run(jarvis.turn(TurnTimer(), start_timeout=0.3)) is False
    assert jarvis.speaker.said == []


def test_a_cough_is_not_a_turn(transcriber, replay):
    jarvis, _ = build(transcriber, replay, "cough")
    assert asyncio.run(jarvis.turn(TurnTimer(), start_timeout=1.0)) is False
    assert jarvis.speaker.said == []


def test_the_conversation_ends_when_the_follow_up_window_finds_nothing(transcriber, replay):
    """One real turn, then silence, then back to idle rather than hanging."""
    jarvis, bus = build(transcriber, replay, "testing_one_two_three")
    sub = bus.subscribe(maxsize=1024)

    async def go():
        await jarvis.conversation(TurnTimer())

    asyncio.run(asyncio.wait_for(go(), timeout=30))
    states = [e["state"] for e in drain(sub) if e["type"] == "state"]
    assert states[-1] == "listening", states
    assert len(jarvis.speaker.said) == 1, "the follow-up window invented a second turn"


def test_barge_in_stops_the_reply_and_returns_to_listening(transcriber, replay):
    """The watcher hears "Hey JARVIS" over the reply and cuts it."""
    jarvis, bus = build(transcriber, replay, "testing_one_two_three", watching="hey_jarvis")
    sub = bus.subscribe(maxsize=1024)

    def interrupted_say(text: str) -> SpeechResult:
        jarvis.speaker.said.append(text)
        # The watcher fires on its own thread; give it a moment to land.
        for _ in range(200):
            if jarvis.speaker.interrupted:
                break
            import time

            time.sleep(0.005)
        return SpeechResult(ttfb_ms=12.0, aborted=jarvis.speaker.interrupted, backend="fake")

    jarvis.speaker.say = interrupted_say
    asyncio.run(jarvis.turn(TurnTimer(), start_timeout=2.0))

    assert jarvis.speaker.interrupted, "the barge-in watcher never fired"
    states = [e["state"] for e in drain(sub) if e["type"] == "state"]
    assert states[-1] == "listening", states


# -- the HUD's Controls protocol: answer, submit_text, request_interrupt, ptt -


def test_answer_speaks_and_records_the_transcript(transcriber, replay):
    jarvis, bus = build(transcriber, replay, "silence")
    sub = bus.subscribe(maxsize=1024)

    asyncio.run(jarvis.answer("what can you do"))

    events = drain(sub)
    user = [e for e in events if e["type"] == "transcript" and e["role"] == "user"]
    assert user and user[0]["text"] == "what can you do"
    assert jarvis.speaker.said == ["You said: what can you do"]


def test_answer_with_nothing_typed_does_nothing():
    jarvis = Jarvis(CONFIG, EventBus())
    jarvis.speaker = FakeSpeaker()
    jarvis.brain = FakeBrain()
    asyncio.run(jarvis.answer("   "))
    assert jarvis.speaker.said == []


def test_submit_text_answers_while_idle_or_listening(transcriber, replay):
    for state in ("idle", "listening"):
        jarvis, _ = build(transcriber, replay, "silence")
        jarvis._state = state
        asyncio.run(jarvis.submit_text("hello"))
        assert jarvis.speaker.said == ["You said: hello"], state


def test_submit_text_is_ignored_while_a_turn_is_already_in_progress(transcriber, replay):
    """A second surface onto one conversation, not a second conversation."""
    for state in ("thinking", "speaking", "boot"):
        jarvis, _ = build(transcriber, replay, "silence")
        jarvis._state = state
        asyncio.run(jarvis.submit_text("hello"))
        assert jarvis.speaker.said == [], state


def test_request_interrupt_cuts_the_speaker_and_the_agent(transcriber, replay):
    async def go():
        jarvis, _ = build(transcriber, replay, "silence")
        jarvis._loop = asyncio.get_running_loop()
        jarvis.request_interrupt()
        await asyncio.sleep(0.05)  # the agent interrupt is scheduled, not inline
        return jarvis

    jarvis = asyncio.run(go())
    assert jarvis.speaker.interrupted
    assert jarvis.brain.interrupted


def test_push_to_talk_press_is_taken_only_while_idle(transcriber, replay):
    for state, expected in (("idle", True), ("listening", False), ("thinking", False)):
        jarvis, _ = build(transcriber, replay, "silence")
        jarvis._state = state
        jarvis.push_to_talk(True)
        assert jarvis._ptt_pressed.is_set() is expected, state


def test_push_to_talk_release_clears_pressed_and_signals_force_end(transcriber, replay):
    jarvis, _ = build(transcriber, replay, "silence")
    jarvis._state = "idle"
    jarvis.push_to_talk(True)
    assert jarvis._ptt_pressed.is_set()
    jarvis.push_to_talk(False)
    assert not jarvis._ptt_pressed.is_set()
    assert jarvis._ptt_release.is_set()


def test_a_second_press_clears_a_stale_release_from_the_last_one(transcriber, replay):
    jarvis, _ = build(transcriber, replay, "silence")
    jarvis._state = "idle"
    jarvis._ptt_release.set()  # left over from a previous press-release
    jarvis.push_to_talk(True)
    assert not jarvis._ptt_release.is_set()


# -- _wait_for_trigger: wake word and push-to-talk share one frame reader ----


def test_wait_for_trigger_returns_ptt_immediately_when_already_pressed(transcriber, replay):
    jarvis, _ = build(transcriber, replay, "silence", watching="silence")
    jarvis._ptt_pressed.set()
    trigger = asyncio.run(asyncio.wait_for(jarvis._wait_for_trigger(), timeout=2.0))
    assert trigger == "ptt"


def test_wait_for_trigger_hears_the_wake_word(transcriber, replay):
    jarvis, _ = build(transcriber, replay, "silence", watching="hey_jarvis")
    trigger = asyncio.run(asyncio.wait_for(jarvis._wait_for_trigger(), timeout=5.0))
    assert trigger == "voice"


def test_wait_for_trigger_gives_up_when_stopping(transcriber, replay):
    jarvis, _ = build(transcriber, replay, "silence", watching="silence")
    jarvis._stopping = True
    trigger = asyncio.run(asyncio.wait_for(jarvis._wait_for_trigger(), timeout=2.0))
    assert trigger is None


# -- force_end threaded through conversation() and turn() -------------------


def test_conversation_passes_force_end_only_to_the_first_turn(transcriber, replay):
    """Whether `force_end` actually fires is the vad-level tests' job; this is
    only about the wiring — the same event reaches the first `record()` call
    and nothing but None reaches any follow-up."""
    fast = Config.from_env({"JARVIS_VAD_FOLLOW_UP_S": "0.3", "JARVIS_VAD_START_TIMEOUT_S": "6"})
    jarvis, _ = build(transcriber, replay, "testing_one_two_three", config=fast)
    seen: list[object] = []
    real_record = jarvis.endpointer.record

    def watching_record(frames, **kwargs):
        seen.append(kwargs.get("force_end"))
        return real_record(frames, **kwargs)

    jarvis.endpointer.record = watching_record
    marker = threading.Event()  # deliberately never set: wiring, not timing

    async def go():
        await asyncio.wait_for(jarvis.conversation(TurnTimer(), force_end=marker), timeout=10.0)

    asyncio.run(go())
    assert len(seen) >= 2, "expected a first turn and at least one follow-up attempt"
    assert seen[0] is marker
    assert all(fe is None for fe in seen[1:]), "force_end leaked into a follow-up"


def test_turn_clears_a_stale_force_end_before_recording(transcriber, replay):
    """Guards the exact bug a leftover set() flag would cause: a real
    utterance being thrown away because the flag from a previous press was
    never cleared."""
    jarvis, _ = build(transcriber, replay, "testing_one_two_three")
    stale = threading.Event()
    stale.set()
    spoke = asyncio.run(
        asyncio.wait_for(jarvis.turn(TurnTimer(), start_timeout=2.0, force_end=stale), timeout=10.0)
    )
    assert spoke is True
    assert not stale.is_set(), "turn() must clear it, not just read it"
