"""
Endpointing against real recorded speech. The fixtures are synthesised by
`scripts/fixtures.py` rather than spoken, which is enough to catch the bug that
actually happens — the framing maths going wrong — but not enough to prove
anything about a person in a room. Only speaking at it proves that.

The padding in every fixture is room noise rather than digital silence, on the
grounds that a detector which only works against pure zeros only works in tests.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from jarvis.audio.vad import Endpointer
from jarvis.config import Config

RATE = 16000


def endpointer(**over: str) -> Endpointer:
    return Endpointer(Config.from_env(over))


def test_it_records_a_whole_utterance_and_stops_after_the_silence(replay):
    result = endpointer().record(replay("testing_one_two_three"), start_timeout=2.0)
    assert result is not None
    assert result.reason == "silence"
    assert 1.0 < result.seconds < 2.7, result.seconds
    assert result.speech_ms >= 250


def test_silence_alone_is_never_an_utterance(replay):
    assert endpointer().record(replay("silence"), start_timeout=0.5) is None


def test_a_cough_is_discarded(replay):
    """150ms of noise is under the 250ms of speech an utterance has to have."""
    assert endpointer().record(replay("cough"), start_timeout=1.0) is None


def test_it_gives_up_promptly_when_nobody_starts_talking(replay):
    """The follow-up window depends on this: it must not wait out the audio."""
    started = time.monotonic()
    assert endpointer().record(replay("silence"), start_timeout=0.2) is None
    assert time.monotonic() - started < 1.0


def test_the_silence_window_is_the_knob_it_is_advertised_as(replay):
    """A longer window keeps recording through the gaps between sentences."""
    short = endpointer(JARVIS_VAD_SILENCE_MS="200").record(
        replay("count_to_five"), start_timeout=2.0
    )
    long = endpointer(JARVIS_VAD_SILENCE_MS="900").record(
        replay("count_to_five"), start_timeout=2.0
    )
    assert short is not None and long is not None
    assert long.seconds > short.seconds, (short.seconds, long.seconds)


def test_the_pre_roll_already_queued_survives_into_the_recording(replay):
    """Whatever is buffered when recording starts is kept, not thrown away."""
    source = replay("testing_one_two_three")
    result = endpointer().record(source, start_timeout=2.0)
    assert result is not None
    # The fixture opens with 300ms of room noise; it must still be at the front.
    lead_in = result.audio[: int(RATE * 0.2)]
    assert np.abs(lead_in).max() < 3000, "the quiet lead-in was trimmed away"


def test_it_re_slices_eighty_millisecond_blocks_into_twenty(audio):
    """webrtcvad accepts 10, 20 or 30ms and nothing else. This is that check."""
    ep = endpointer()
    block = audio("testing_one_two_three")[:1280]
    subs = ep._sub_frames(block)
    assert len(subs) == 4
    assert all(len(s) == 320 for s in subs)


def test_speech_is_distinguishable_from_room_noise(audio):
    ep = endpointer()
    assert ep.speech_ratio(audio("testing_one_two_three")) > 0.3
    assert ep.speech_ratio(audio("silence")) < 0.1


def test_a_long_utterance_is_capped_rather_than_running_forever(replay):
    """Someone who never stops talking still gets transcribed at some point."""
    ep = endpointer(JARVIS_VAD_MAX_UTTERANCE_S="2.0", JARVIS_VAD_SILENCE_MS="5000")
    result = ep.record(replay("count_to_five"), start_timeout=2.0)
    assert result is not None
    assert result.reason == "max_length"
    assert 1.9 < result.seconds < 2.2


def test_the_cap_does_not_manufacture_an_utterance_out_of_a_cough(replay):
    """Cut short enough that under 250ms of it was speech: still discarded."""
    ep = endpointer(JARVIS_VAD_MAX_UTTERANCE_S="1.0", JARVIS_VAD_SILENCE_MS="5000")
    assert ep.record(replay("count_to_five"), start_timeout=2.0) is None


@pytest.mark.parametrize("aggressiveness", ["0", "1", "2", "3"])
def test_every_aggressiveness_setting_still_hears_speech(replay, aggressiveness):
    result = endpointer(JARVIS_VAD_AGGRESSIVENESS=aggressiveness).record(
        replay("testing_one_two_three"), start_timeout=2.0
    )
    assert result is not None, f"aggressiveness {aggressiveness} heard nothing"


class DrippingReplay:
    """Like the `replay` fixture, but paced in real time.

    A normal replay plays a whole fixture back in a few milliseconds of wall
    clock, which is faster than any background thread could act on it. This
    exists solely to test `force_end` (push-to-talk releasing): something has
    to hold `record()` open in real time long enough for a timer to fire
    mid-stream.
    """

    def __init__(self, audio, block: int = 1280, delay: float = 0.01) -> None:
        self._blocks = [audio[i : i + block] for i in range(0, len(audio) - block + 1, block)]
        self._i = 0
        self._delay = delay

    def get(self, timeout: float | None = None) -> np.ndarray | None:
        time.sleep(self._delay)
        if self._i >= len(self._blocks):
            return None
        self._i += 1
        return self._blocks[self._i - 1]


def test_force_end_cuts_the_recording_immediately_rather_than_waiting_out_silence(audio):
    """Push-to-talk releasing: protocol.md says this skips the silence wait."""
    source = DrippingReplay(audio("count_to_five"), delay=0.01)
    force_end = threading.Event()
    # 0.4s of real time: "count_to_five" opens with 300ms of silent lead-in, so
    # anything shorter risks releasing before any speech has actually happened.
    threading.Timer(0.4, force_end.set).start()
    # A silence window this long would never end on its own within the test.
    ep = endpointer(JARVIS_VAD_SILENCE_MS="5000")
    started = time.monotonic()
    result = ep.record(source, start_timeout=2.0, force_end=force_end)
    elapsed = time.monotonic() - started
    assert result is not None
    assert result.reason == "forced"
    assert elapsed < 0.6, f"took {elapsed:.2f}s — force_end was not honoured promptly"


def test_force_end_before_any_speech_is_not_an_utterance_and_does_not_crash(replay):
    """An instantaneous press-release: zero frames collected.

    Guards against a real bug found while adding this: concatenating an empty
    list of frames raises, rather than the "nothing was said" that this is.
    """
    force_end = threading.Event()
    force_end.set()
    assert endpointer().record(replay("silence"), start_timeout=2.0, force_end=force_end) is None
