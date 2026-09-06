"""
Latency over the fixture set, so "it feels slow" becomes a number with a stage
attached to it.

What is real here and what is not: the speech-to-text and text-to-speech numbers
are measured, on this machine, against real audio. The endpoint window is not
measured — it is a constant the user chose (`JARVIS_VAD_SILENCE_MS`), and the
utterance cannot end before it elapses, so it is added as the fixed cost it is.
The agent's time to first token is zero in phase 2 because the brain is a stub
that echoes; phase 3 is where that line stops being free.

    make bench              measure everything, including a real TTS round trip
    make bench ARGS=--no-tts   skip the network, and the quota
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from jarvis.audio import stt as stt_module  # noqa: E402
from jarvis.audio.chunker import SentenceChunker  # noqa: E402
from jarvis.audio.tts import Speaker  # noqa: E402
from jarvis.audio.vad import Endpointer  # noqa: E402
from jarvis.config import CONFIG  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"
# Plain utterances only. `wake_then_question` belongs to the pipeline test: it
# opens with the wake word, so recording it from the top measures the wrong thing.
SPEECH_FIXTURES = [
    "testing_one_two_three",
    "count_to_five",
    "weather_question",
    "no_wake_word",
]


class Replay:
    def __init__(self, audio: np.ndarray, block: int) -> None:
        self._blocks = [audio[i : i + block] for i in range(0, len(audio) - block + 1, block)]
        self._i = 0

    def get(self, timeout: float | None = None) -> np.ndarray | None:
        if self._i >= len(self._blocks):
            return None
        self._i += 1
        return self._blocks[self._i - 1]


def load(name: str) -> np.ndarray:
    with wave.open(str(FIXTURES / f"{name}.wav")) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))
    return ordered[k]


def report(name: str, values: list[float], budget: int | None = None) -> None:
    if not values:
        print(f"  {name:<12} —")
        return
    p50, p95 = percentile(values, 50), percentile(values, 95)
    verdict = ""
    if budget is not None:
        verdict = "  ok" if p50 <= budget else f"  OVER by {p50 - budget:.0f}ms"
    print(
        f"  {name:<12} p50 {p50:>7.0f}ms   p95 {p95:>7.0f}ms   "
        f"mean {statistics.mean(values):>7.0f}ms{verdict}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="latency over the fixture set")
    parser.add_argument("--no-tts", action="store_true", help="skip the network round trip")
    parser.add_argument("--repeat", type=int, default=3, help="passes over each fixture")
    args = parser.parse_args(argv)

    missing = [f for f in SPEECH_FIXTURES if not (FIXTURES / f"{f}.wav").exists()]
    if missing:
        print(f"missing fixtures: {', '.join(missing)} — run `make fixtures`", file=sys.stderr)
        return 1

    print("\nbuilding the pipeline...")
    endpointer = Endpointer(CONFIG)
    transcriber = stt_module.TimedTranscriber(stt_module.build(CONFIG), CONFIG)
    transcriber.warmup()
    speaker = Speaker(CONFIG) if not args.no_tts else None
    if speaker is not None and not speaker._voice.available:
        print("  no elevenlabs voice configured — measuring without tts")
        speaker = None

    endpoint_ms: list[float] = []
    stt_ms: list[float] = []
    ttfb_ms: list[float] = []
    rate = CONFIG.audio.sample_rate
    block = CONFIG.audio.block_samples

    print(f"{len(SPEECH_FIXTURES)} fixtures x {args.repeat} passes\n")
    for name in SPEECH_FIXTURES:
        audio = load(name)
        for _ in range(args.repeat):
            started = time.perf_counter()
            utterance = endpointer.record(Replay(audio, block), start_timeout=2.0)
            processing = (time.perf_counter() - started) * 1000
            if utterance is None:
                print(f"  {name}: no utterance found")
                continue
            # The window itself is a constant the user picked; the CPU cost of
            # detecting it is what varies, so both are reported together.
            endpoint_ms.append(CONFIG.vad.silence_ms + processing)
            text = transcriber.transcribe(utterance.audio, rate)
            stt_ms.append(transcriber.last_ms)
            if speaker is not None:
                reply = f"You said: {text}"
                first = next(iter(SentenceChunker(CONFIG.tts).feed(reply) or [reply]))
                result = speaker.say(first)
                ttfb_ms.append(result.ttfb_ms)
        print(f"  {name:<22} {text!r}")

    print("\nstages")
    report("endpoint", endpoint_ms, CONFIG.vad.silence_ms + 50)
    report("stt", stt_ms, CONFIG.stt.budget_ms)
    report("llm_ttft", [], CONFIG.brain.ttft_budget_ms)
    report("tts_ttfb", ttfb_ms, CONFIG.tts.budget_ms)

    totals = [
        percentile(endpoint_ms, p) + percentile(stt_ms, p) + percentile(ttfb_ms, p)
        for p in (50, 95)
    ]
    print(f"\n  turn         p50 {totals[0]:>7.0f}ms   p95 {totals[1]:>7.0f}ms   budget 1200ms")
    print(
        f"  {'PASS' if totals[0] < 1200 else 'FAIL'}: p50 turn latency "
        f"{'under' if totals[0] < 1200 else 'over'} 1.2s"
    )
    if not ttfb_ms:
        print("  (llm_ttft is phase 3; tts not measured in this run)")
    else:
        print("  (llm_ttft is phase 3 — the stub brain answers instantly)")
    if speaker is not None:
        speaker.stop()
    return 0 if totals[0] < 1200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
