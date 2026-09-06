"""
The wake word against fixtures. Synthesised speech triggers `hey_jarvis` at
about 0.99, which is enough to test the wiring — the framing, the debounce, the
two thresholds — but it is not evidence about anyone's actual voice in an actual
room. Only speaking at it is that, and that is what the phase gate is for.
"""

from __future__ import annotations

import threading

import pytest

from jarvis.audio.wake import WakeWord, strip_wake_phrase
from jarvis.config import Config


@pytest.fixture(scope="module")
def detector() -> WakeWord:
    wake = WakeWord()
    wake.load()
    return wake


def test_it_hears_hey_jarvis(replay, detector):
    score = detector.wait(replay("hey_jarvis"), timeout=5.0)
    assert score is not None and score > 0.5


def test_it_does_not_fire_on_similar_sounding_speech(replay, detector):
    """ "is the JavaScript ready" shares most of its phonemes with the target."""
    detector.reset()
    assert detector.wait(replay("no_wake_word"), timeout=2.0) is None


def test_it_does_not_fire_on_room_noise(replay, detector):
    detector.reset()
    assert detector.wait(replay("silence"), timeout=2.0) is None


def test_the_barge_in_threshold_is_stricter_than_the_idle_one():
    config = Config.from_env({})
    assert config.wake.barge_in_threshold > config.wake.threshold


def test_a_frame_scores_between_zero_and_one(audio, detector):
    frame = audio("hey_jarvis")[:1280]
    score = detector.score(frame)
    assert 0.0 <= score <= 1.0


def test_it_debounces_so_one_wake_word_is_not_three(replay, detector):
    """The model keeps scoring high for a moment after the phrase ends."""
    detector.reset()
    detector._last_trigger = 0.0
    source = replay("hey_jarvis")
    first = detector.wait(source, timeout=5.0)
    assert first is not None
    # Same audio, immediately: the one-second debounce must swallow it.
    again = detector.wait(replay("hey_jarvis"), timeout=1.0)
    assert again is None


def test_barge_in_watching_stops_when_told_to(replay, detector):
    detector.reset()
    fired = threading.Event()
    stop = threading.Event()
    thread = detector.watch_for_barge_in(replay("silence"), stop, on_trigger=fired.set)
    stop.set()
    thread.join(timeout=3.0)
    assert not thread.is_alive(), "the watcher outlived its stop signal"
    assert not fired.is_set()


def test_barge_in_fires_on_the_wake_word_and_calls_back(replay, detector):
    detector.reset()
    detector._last_trigger = 0.0
    fired = threading.Event()
    stop = threading.Event()
    thread = detector.watch_for_barge_in(replay("hey_jarvis"), stop, on_trigger=fired.set)
    thread.join(timeout=10.0)
    stop.set()
    assert fired.is_set(), "barge-in did not trigger on the wake word"


@pytest.mark.parametrize(
    ("heard", "asked"),
    [
        # The detector fires at the end of the phrase and the pre-roll reaches
        # back past it, so these are what actually arrives from the recogniser.
        ("Hey Jarvis, what's the weather today?", "what's the weather today?"),
        ("Hey Jervis. What's my battery at?", "What's my battery at?"),
        ("Hey Javis, open Spotify.", "open Spotify."),
        ("hey jarvis whats my battery at", "whats my battery at"),
        ("Jarvis, turn it down a bit.", "turn it down a bit."),
        ("Hey Jervis.", ""),
        # ...and these must survive untouched.
        ("What is the weather today?", "What is the weather today?"),
        ("Tell Sarah hey jarvis is working", "Tell Sarah hey jarvis is working"),
        ("Testing one, two, three.", "Testing one, two, three."),
    ],
)
def test_the_wake_phrase_is_stripped_but_only_from_the_front(heard, asked):
    assert strip_wake_phrase(heard) == asked


def test_check_frame_is_the_building_block_wait_is_made_of(replay, detector):
    """main.py polls frame-by-frame with this, to weave in a push-to-talk press
    without a second, uncoordinated reader on the same frame queue."""
    detector.reset()
    detector._last_trigger = 0.0
    source = replay("hey_jarvis")
    scores = []
    while True:
        frame = source.get(timeout=0.1)
        if frame is None:
            break
        scores.append(detector.check_frame(frame))
    hits = [s for s in scores if s is not None]
    assert len(hits) == 1, "the debounce should still collapse repeats"
    assert hits[0] > 0.5


def test_check_frame_respects_a_custom_threshold(replay, detector):
    """The model is stateful across a stream, so this replays the fixture
    properly rather than scoring one frame in isolation."""

    def peak_over(threshold):
        detector.reset()
        detector._last_trigger = 0.0
        source = replay("hey_jarvis")
        hit = None
        while True:
            frame = source.get(timeout=0.1)
            if frame is None:
                return hit
            score = detector.check_frame(frame, threshold=threshold)
            if score is not None:
                hit = score

    assert peak_over(1.01) is None, "an impossible threshold must never trigger"
    assert peak_over(0.5) is not None, "the default-ish threshold should still catch it"
