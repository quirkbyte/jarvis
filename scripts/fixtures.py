"""
Builds the test fixtures with macOS `say`, so they are reproducible on any Mac
rather than checked in as opaque audio nobody can regenerate. Run it with
`make fixtures`.

Synthesised speech is not the same as a person in a room, and the tests that use
these say so: they prove the pipeline is wired correctly, not that the wake word
triggers on *your* voice. Only speaking at it proves that. What synthesis buys
is a fixture set that fails loudly when someone breaks the framing maths, which
is the bug that actually happens.

The padding is deliberately not digital silence — a real room has a noise floor,
and a VAD that only works against pure zeros is a VAD that only works in tests.
"""

from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
RATE = 16000
NOISE_DBFS = -55.0  # a quiet room, not a vacuum
SPEECH_DBFS = -20.0  # `say` comes out at about -4 dBFS; no microphone does

# name, spoken text, voice, lead-in silence (ms), trailing silence (ms)
SPOKEN = [
    ("hey_jarvis", "Hey JARVIS", "Daniel", 500, 500),
    ("wake_then_question", "Hey JARVIS. What's my battery at?", "Daniel", 400, 900),
    ("testing_one_two_three", "Testing one two three", "Daniel", 300, 900),
    ("count_to_five", "One. Two. Three. Four. Five.", "Daniel", 300, 900),
    ("no_wake_word", "Hey there, is the JavaScript ready yet?", "Alex", 400, 700),
    ("weather_question", "What's the weather in London tomorrow?", "Samantha", 300, 900),
]


def noise(ms: int, dbfs: float = NOISE_DBFS, seed: int = 0) -> np.ndarray:
    """Room tone, not white noise: real rooms are low-frequency dominated, and a
    detector tuned against white noise is tuned against something that does not
    exist outside a test."""
    rng = np.random.default_rng(seed)
    raw = rng.normal(0.0, 1.0, int(RATE * ms / 1000))
    smoothed = np.zeros_like(raw)
    accumulator = 0.0
    for i, value in enumerate(raw):
        accumulator += 0.05 * (value - accumulator)
        smoothed[i] = accumulator
    smoothed /= max(float(np.sqrt(np.mean(smoothed**2))), 1e-12)
    return (smoothed * 10 ** (dbfs / 20) * 32768).astype(np.int16)


def at_level(pcm: np.ndarray, dbfs: float = SPEECH_DBFS) -> np.ndarray:
    """Scale speech to a level a microphone would actually deliver."""
    peak = float(np.abs(pcm).max()) / 32768.0
    if peak <= 0:
        return pcm
    return (pcm * (10 ** (dbfs / 20) / peak)).astype(np.int16)


def say(text: str, voice: str) -> np.ndarray:
    out = FIXTURES / ".say.wav"
    subprocess.run(
        [
            "say",
            "-v",
            voice,
            "--data-format=LEI16@16000",
            "--file-format=WAVE",
            "-o",
            str(out),
            text,
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    with wave.open(str(out)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    out.unlink()
    return pcm


def write(name: str, pcm: np.ndarray) -> Path:
    path = FIXTURES / f"{name}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return path


def build() -> list[Path]:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    written = []
    for i, (name, text, voice, lead, tail) in enumerate(SPOKEN):
        speech = at_level(say(text, voice))
        padded = np.concatenate([noise(lead, seed=i), speech, noise(tail, seed=i + 100)])
        written.append(write(name, padded))

    # Two synthetic ones. A cough is 150ms of broadband noise — under the 250ms
    # of speech the endpointer demands, which is exactly the point of it.
    rng = np.random.default_rng(7)
    cough = at_level(
        (rng.normal(0, 0.15, int(RATE * 0.15)) * 32768).clip(-32768, 32767).astype(np.int16)
    )
    written.append(write("cough", np.concatenate([noise(400), cough, noise(800)])))
    written.append(write("silence", noise(2000, seed=42)))
    return written


def main() -> int:
    if sys.platform != "darwin":
        print("fixtures are built with macOS `say`", file=sys.stderr)
        return 1
    for path in build():
        with wave.open(str(path)) as w:
            seconds = w.getnframes() / w.getframerate()
        print(f"  {path.relative_to(REPO_ROOT)}  {seconds:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
