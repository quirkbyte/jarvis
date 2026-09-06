"""
Auditions British voices and writes the winner to `.env`.

There is deliberately no voice ID written down anywhere in this repo.
ElevenLabs is retiring its old default voice set at the end of 2026, so any ID
hardcoded today is a time bomb; discovery at runtime is the only approach that
still works next year. What is fixed is the *character* being auditioned for —
male, RP rather than regional, 30 to 50, described as calm, measured, narrator
or documentary. Anything labelled energetic or conversational reads as a podcast
host, and JARVIS is not a podcast host.

The audition line is chosen to exercise the register we actually want: a piece
of bad news delivered without drama, and a question at the end.

    python scripts/voices.py            audition every candidate, then choose
    python scripts/voices.py --list     just show what the account can see
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from jarvis.config import CONFIG  # noqa: E402

AUDITION_LINE = (
    "Good evening. The build finished about four minutes ago; "
    "two of the tests failed. Shall I read them out?"
)

# Labels that disqualify a voice for this role, whatever else it says.
WRONG_REGISTER = {"energetic", "upbeat", "conversational", "excited", "hyped"}


def client():
    from elevenlabs.client import ElevenLabs

    if not CONFIG.tts.api_key:
        raise SystemExit("ELEVENLABS_API_KEY is not set — put it in .env")
    return ElevenLabs(api_key=CONFIG.tts.api_key)


def candidates(api) -> list:
    """British voices, and anything the account holder made themselves.

    A cloned voice carries no `accent` label at all — filtering on accent alone
    silently hides the one voice someone deliberately made for this. The old
    code also only fell back to an unfiltered scan when the accent search came
    back *empty*, which never happens once any premade British voice exists on
    the account, so that fallback was dead code the moment George showed up.
    """
    voices = api.voices.search(page_size=100).voices
    return sorted((v for v in voices if _is_relevant(v)), key=_fitness)


def _is_relevant(voice) -> bool:
    labels = getattr(voice, "labels", {}) or {}
    if str(labels.get("language", "en")).lower() not in ("en", ""):
        return False  # e.g. a clone made for another language entirely
    if str(getattr(voice, "category", "")) == "cloned":
        return True
    return "british" in str(labels.get("accent", "")).lower()


def _fitness(voice) -> tuple[int, str]:
    """Lower sorts first. A personal clone first, then male/mature/narrative."""
    labels = {k: str(v).lower() for k, v in (getattr(voice, "labels", {}) or {}).items()}
    descriptive = labels.get("descriptive", "")
    score = 0
    if str(getattr(voice, "category", "")) == "cloned":
        score -= 10  # made for exactly this; outranks any premade voice
    score -= 4 if labels.get("gender") == "male" else 0
    score -= 2 if labels.get("age") in {"middle_aged", "old"} else 0
    score -= 2 if descriptive in {"mature", "formal", "calm", "measured"} else 0
    score -= 1 if "narrat" in labels.get("use_case", "") else 0
    score += 5 if descriptive in WRONG_REGISTER else 0
    return score, str(voice.name)


def describe(voice) -> str:
    labels = getattr(voice, "labels", {}) or {}
    category = str(getattr(voice, "category", ""))
    bits = [str(labels.get(k, "")) for k in ("gender", "age", "descriptive", "use_case")]
    if category and category != "premade":
        bits.insert(0, category)
    tags = ", ".join(b for b in bits if b)
    return f"{voice.name}  [{tags}]" if tags else voice.name


def audition(api, voice) -> None:
    """Synthesise the line in this voice and play it through the speakers."""
    import numpy as np
    import sounddevice as sd

    rate = CONFIG.tts.sample_rate
    chunks = api.text_to_speech.stream(
        voice_id=voice.voice_id,
        text=AUDITION_LINE,
        model_id=CONFIG.tts.model,
        output_format=CONFIG.tts.output_format,
    )
    pcm = np.frombuffer(b"".join(chunks), dtype=np.int16)
    sd.play(pcm, samplerate=rate, blocking=True)


def write_env(voice_id: str, name: str, path: Path | None = None) -> Path:
    """Set ELEVENLABS_VOICE_ID in .env, replacing any existing line."""
    path = path or REPO_ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    drop = ("ELEVENLABS_VOICE_ID=", "ELEVENLABS_VOICE_NAME=")
    kept = [ln for ln in lines if not ln.strip().startswith(drop)]
    kept.append(f"ELEVENLABS_VOICE_ID={voice_id}")
    kept.append(f"ELEVENLABS_VOICE_NAME={name.split(' - ')[0].strip()}")
    path.write_text("\n".join(kept).strip() + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="audition British voices for JARVIS")
    parser.add_argument("--list", action="store_true", help="list candidates and stop")
    parser.add_argument("--voice", help="audition one voice id and stop")
    args = parser.parse_args(argv)

    api = client()
    voices = candidates(api)
    if not voices:
        print("no British voices visible on this account", file=sys.stderr)
        return 1

    print(f"\n{len(voices)} British voices, best fit for the role first:\n")
    for i, voice in enumerate(voices, 1):
        print(f"  {i}. {describe(voice)}   {voice.voice_id}")

    if args.list:
        return 0
    if args.voice:
        match = next((v for v in voices if v.voice_id == args.voice), None)
        if match is None:
            print(f"no such voice: {args.voice}", file=sys.stderr)
            return 1
        audition(api, match)
        return 0

    print(f'\naudition line:\n  "{AUDITION_LINE}"\n')
    for i, voice in enumerate(voices, 1):
        input(f"  [{i}/{len(voices)}] {voice.name} — enter to play, ctrl-c to stop ")
        audition(api, voice)

    choice = input(f"\nwhich one? 1-{len(voices)}, or enter to keep the current setting: ").strip()
    if not choice:
        return 0
    try:
        chosen = voices[int(choice) - 1]
    except (ValueError, IndexError):
        print("not one of the options", file=sys.stderr)
        return 1
    path = write_env(chosen.voice_id, str(chosen.name))
    print(f"\n{chosen.name} written to {path.relative_to(REPO_ROOT)} — `make run` will use it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
