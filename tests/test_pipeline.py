"""
The three audio stages joined up, on real audio, in the order the assistant uses
them: hear the wake word, record what follows, transcribe it. Each stage is
tested alone elsewhere; what this catches is the seam — a sample rate assumed in
one place and not another, or a pre-roll thrown away between two components that
each behave correctly on their own.

The transcription assertions are deliberately loose. Speech-to-text normalises
as it goes ("one two three" comes back as "123"), and a test that pins the exact
string is a test that fails the next time the model improves.
"""

from __future__ import annotations

import math
import threading
import time

import pytest

from jarvis.audio import stt as stt_module
from jarvis.audio.mic import FrameQueue
from jarvis.audio.vad import Endpointer
from jarvis.audio.wake import WakeWord
from jarvis.config import CONFIG


@pytest.fixture(scope="module")
def transcriber():
    if not CONFIG.is_apple_silicon:
        pytest.skip("parakeet needs apple silicon")
    built = stt_module.TimedTranscriber(stt_module.build(CONFIG), CONFIG)
    built.warmup()
    return built


def digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


def words(text: str) -> set[str]:
    return {w.strip(".,?!").lower() for w in text.split()}


def test_wake_word_then_record_then_transcribe(replay, transcriber):
    """The whole path: "Hey JARVIS. What's my battery at?" in one fixture."""
    source = replay("wake_then_question")
    wake = WakeWord()
    wake.load()
    assert wake.wait(source, timeout=5.0) is not None, "never heard the wake word"

    # The same source continues — exactly as the live loop does.
    utterance = Endpointer(CONFIG).record(source, start_timeout=2.0)
    assert utterance is not None, "heard the wake word, then recorded nothing"
    assert utterance.speech_ms >= 250

    text = transcriber.transcribe(utterance.audio, CONFIG.audio.sample_rate)
    assert "battery" in words(text), text


def test_the_wake_phrase_does_not_end_up_in_the_question(audio, transcriber):
    """The bug this pins: the recogniser hearing "Hey JARVIS" as part of the ask.

    The detector fires at the end of the phrase, so the audio buffered behind
    the trigger is the word itself. Recording it produced transcripts like
    "HRVs testing one two three" and "Discount slowly from one to twenty" —
    which no amount of prefix-stripping reliably undoes, because the recogniser
    mangles the fragment differently every time.

    Built on the real FrameQueue rather than a replay double, because the whole
    mechanism *is* the buffering: one queue is drained by the wake detector
    while the other quietly accumulates the same frames behind it.
    """
    block = CONFIG.audio.block_samples
    pcm = audio("wake_then_question")
    frames = [pcm[i : i + block] for i in range(0, len(pcm) - block + 1, block)]

    wake = WakeWord()
    wake.load()
    recorder = FrameQueue(math.ceil(CONFIG.vad.pre_roll_ms / CONFIG.audio.block_ms), "rec")

    fired = None
    for i, frame in enumerate(frames):
        recorder._offer(frame)  # accumulates behind the detector, as the mic does
        if wake.score(frame) > CONFIG.wake.threshold:
            fired = i
            break
    assert fired is not None, "never heard the wake word"
    assert len(recorder) > 1, "nothing was buffered behind the trigger"

    keep = max(1, CONFIG.vad.wake_pre_roll_ms // CONFIG.audio.block_ms)
    dropped = recorder.trim(keep)
    assert dropped > 0, "there was wake-word audio to drop and none was dropped"

    # Keep feeding it while the endpointer runs, as the capture thread would.
    def feed() -> None:
        for frame in frames[fired + 1 :]:
            recorder._offer(frame)
            time.sleep(0.005)
        time.sleep(0.3)
        recorder.close()

    threading.Thread(target=feed, daemon=True).start()
    utterance = Endpointer(CONFIG).record(recorder, start_timeout=3.0)
    assert utterance is not None, "recorded nothing after the wake word"

    text = transcriber.transcribe(utterance.audio, CONFIG.audio.sample_rate)
    assert "battery" in words(text), f"the question was lost: {text!r}"
    leaked = {"jarvis", "jervis", "javis", "hey", "hrvs", "this"} & words(text)
    assert not leaked, f"the wake phrase leaked into {text!r} as {leaked}"


def test_transcription_of_a_plain_utterance(replay, transcriber):
    utterance = Endpointer(CONFIG).record(replay("testing_one_two_three"), start_timeout=2.0)
    assert utterance is not None
    text = transcriber.transcribe(utterance.audio, CONFIG.audio.sample_rate)
    assert "testing" in words(text), text
    assert digits(text) == "123" or words(text) >= {"one", "two", "three"}, text


def test_transcription_is_within_its_budget(replay, transcriber):
    """The wrong model here is the commonest cause of a sluggish assistant."""
    utterance = Endpointer(CONFIG).record(replay("weather_question"), start_timeout=2.0)
    assert utterance is not None
    transcriber.transcribe(utterance.audio, CONFIG.audio.sample_rate)
    assert transcriber.last_ms < CONFIG.stt.budget_ms * 3, (
        f"{transcriber.last_ms:.0f}ms on {utterance.seconds:.1f}s of audio"
    )


def test_silence_produces_no_utterance_and_so_never_reaches_the_model(replay):
    assert Endpointer(CONFIG).record(replay("silence"), start_timeout=0.5) is None
