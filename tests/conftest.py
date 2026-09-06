"""Shared fixture loading. The WAVs are built by `make fixtures`."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> np.ndarray:
    with wave.open(str(FIXTURES / f"{name}.wav")) as w:
        assert w.getframerate() == 16000, f"{name}: fixtures are 16kHz mono int16"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


class ReplayQueue:
    """Feeds a recorded WAV through the same interface as the microphone.

    Blocks are handed out instantly rather than in real time, so the tests run
    at the speed of the code and not the speed of the audio. `get` returns None
    once the fixture runs out, which is what the mic does on a timeout — so the
    endpointer's give-up paths get exercised too.
    """

    def __init__(self, audio: np.ndarray, block: int = 1280) -> None:
        self._blocks = [audio[i : i + block] for i in range(0, len(audio) - block + 1, block)]
        self._index = 0
        self.exhausted = False

    def get(self, timeout: float | None = None) -> np.ndarray | None:
        if self._index >= len(self._blocks):
            self.exhausted = True
            return None
        self._index += 1
        return self._blocks[self._index - 1]

    def clear(self) -> None:
        """The live mic drops frames captured while nobody was listening.

        A fixture has no stale audio — every block is the recording under test —
        so this is deliberately a no-op rather than a skip-to-the-end.
        """


@pytest.fixture
def audio():
    return load


@pytest.fixture
def replay():
    return lambda name, **kw: ReplayQueue(load(name), **kw)
