"""
Speech to text, behind an interface thin enough that swapping the backend is a
config change. On Apple Silicon that is parakeet-mlx, which transcribes a
1.5-second utterance in about 150ms on the GPU — exactly BUILD.md's budget.
faster-whisper is the fallback for Intel and for anyone without MLX.

Two things here are not incidental. The first inference costs 4.7 seconds while
the model warms up, and the first inference is the user's first sentence, so
`warmup()` pays that at boot against a fixture instead. The second is the
warning when a transcription blows its budget: the wrong model here is the most
common cause of an assistant that feels sluggish, and it is completely invisible
unless something says so out loud.

parakeet's stable interface takes a file path, so utterances go to a temp WAV.
On a Mac that write costs well under a millisecond against 150ms of inference.

MLX arrays belong to the thread that made them. The decoder keeps hidden state
between calls, so transcribing from a different thread than the one that warmed
the model fails with "there is no Stream(gpu, 2) in current thread" — which is
exactly what `asyncio.to_thread` does. Every MLX call therefore goes through one
dedicated worker owned by the backend, and the caller waits on it.
"""

from __future__ import annotations

import logging
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np

from jarvis.config import CONFIG, Config

if TYPE_CHECKING:
    from numpy.typing import NDArray

log = logging.getLogger("jarvis.stt")

WARMUP_TEXT = "testing one two three"


class Transcriber(Protocol):
    name: str

    def load(self) -> None: ...
    def warmup(self) -> None: ...
    def transcribe(self, audio: NDArray[np.int16], rate: int) -> str: ...
    def close(self) -> None: ...


def write_wav(audio: NDArray[np.int16], rate: int, path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.tobytes())
    return path


class ParakeetMLX:
    """Apple Silicon, on the GPU, fully local."""

    name = "ParakeetMLX"

    def __init__(self, config: Config = CONFIG) -> None:
        self._config = config.stt
        self._model = None
        self._dir = Path(tempfile.mkdtemp(prefix="jarvis-stt-"))
        # One worker, for the life of the object: see the module docstring.
        self._mlx = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-mlx")

    def load(self) -> None:
        self._mlx.submit(self._load_here).result()

    def _load_here(self) -> None:
        if self._model is not None:
            return
        from parakeet_mlx import from_pretrained

        started = time.perf_counter()
        self._model = from_pretrained(self._config.model)
        log.info("stt %s loaded in %.1fs", self._config.model, time.perf_counter() - started)

    def warmup(self) -> None:
        """The first transcription costs ~4.7s. Spend it before it is needed."""
        started = time.perf_counter()
        self.transcribe(np.zeros(16000, dtype=np.int16), 16000)
        log.info("stt warm in %.1fs", time.perf_counter() - started)

    def transcribe(self, audio: NDArray[np.int16], rate: int) -> str:
        path = write_wav(audio, rate, self._dir / "utterance.wav")
        return self._mlx.submit(self._transcribe_here, path).result()

    def _transcribe_here(self, path: Path) -> str:
        self._load_here()
        assert self._model is not None
        return str(self._model.transcribe(path).text).strip()

    def close(self) -> None:
        self._mlx.shutdown(wait=False, cancel_futures=True)


class FasterWhisper:
    """Anywhere. Slower, but it does not need MLX."""

    name = "FasterWhisper"

    def __init__(self, config: Config = CONFIG) -> None:
        self._config = config.stt
        self._model = None

    def load(self) -> None:
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        self._model = WhisperModel(
            self._config.whisper_model,
            device="cpu",
            compute_type=self._config.whisper_compute_type,
        )

    def warmup(self) -> None:
        self.load()
        self.transcribe(np.zeros(16000, dtype=np.int16), 16000)

    def close(self) -> None:
        self._model = None

    def transcribe(self, audio: NDArray[np.int16], rate: int) -> str:
        self.load()
        assert self._model is not None
        samples = audio.astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(
            samples, language=self._config.language, beam_size=self._config.beam_size
        )
        return " ".join(s.text for s in segments).strip()


BACKENDS: dict[str, type] = {"parakeet": ParakeetMLX, "whisper": FasterWhisper}


def build(config: Config = CONFIG) -> Transcriber:
    """The configured backend, falling back rather than failing at boot."""
    order = [config.stt.backend] + [b for b in BACKENDS if b != config.stt.backend]
    errors = []
    for backend in order:
        cls = BACKENDS.get(backend)
        if cls is None:
            continue
        if backend == "parakeet" and not config.is_apple_silicon:
            continue
        try:
            transcriber: Transcriber = cls(config)
            transcriber.load()
            return transcriber
        except Exception as exc:  # noqa: BLE001 - try the next one, then report all
            log.warning("stt backend %s unavailable: %s", backend, exc)
            errors.append(f"{backend}: {exc}")
    raise RuntimeError("no speech-to-text backend available — " + "; ".join(errors))


class TimedTranscriber:
    """Wraps a backend to time it and complain when it misses its budget."""

    def __init__(self, inner: Transcriber, config: Config = CONFIG) -> None:
        self._inner = inner
        self._config = config
        self.last_ms = 0.0

    @property
    def name(self) -> str:
        return self._inner.name

    def warmup(self) -> None:
        self._inner.warmup()

    def close(self) -> None:
        self._inner.close()

    def transcribe(self, audio: NDArray[np.int16], rate: int) -> str:
        started = time.perf_counter()
        text = self._inner.transcribe(audio, rate)
        self.last_ms = (time.perf_counter() - started) * 1000
        budget = self._config.stt.budget_ms
        if self.last_ms > budget:
            faster = "parakeet-mlx" if self.name != "ParakeetMLX" else "a smaller model"
            log.warning(
                "stt took %.0fms against a %dms budget on %.1fs of audio — try %s",
                self.last_ms,
                budget,
                len(audio) / rate,
                faster,
            )
        return text
