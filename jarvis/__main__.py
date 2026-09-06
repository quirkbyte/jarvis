"""
The entry point. It is intentionally thin: parse arguments, build config, start
logging, hand off. Phase 1 has no voice loop to hand off to yet, so the run
modes say which phase brings them rather than pretending to work — a mode that
half-runs is worse than one that tells you the truth and exits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from jarvis import __version__
from jarvis.config import CONFIG, Config
from jarvis.logging import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis", description="JARVIS voice assistant")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    parser.add_argument("--config", action="store_true", help="print resolved config as JSON")
    parser.add_argument("--doctor", action="store_true", help="run the environment check")
    parser.add_argument("--text", action="store_true", help="text mode, no microphone")
    parser.add_argument(
        "--continue",
        dest="resume",
        action="store_true",
        help="pick up the last conversation instead of starting a new one",
    )
    parser.add_argument(
        "--no-hud", action="store_true", help="skip the HUD server (JARVIS_HUD_ENABLED=0)"
    )
    parser.add_argument("--bench", action="store_true", help="latency report over fixtures")
    parser.add_argument("-v", "--verbose", action="store_true", help="per-stage timing detail")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # CONFIG is built once, at import — before argv exists to read. A CLI flag
    # that only sets the environment variable is too late to affect it, which
    # is a bug `-v` had for four phases: it changed the environment and then
    # nothing ever looked at the environment again. Rebuild when a flag needs to.
    if args.verbose:
        os.environ["JARVIS_VERBOSE"] = "1"
    if args.no_hud:
        os.environ["JARVIS_HUD_ENABLED"] = "0"
    config = Config.from_env() if (args.verbose or args.no_hud) else CONFIG
    setup_logging(config)

    if args.config:
        print(json.dumps(config.describe(), indent=2, sort_keys=True))
        return 0

    if args.doctor:
        from scripts.doctor import main as doctor_main

        return doctor_main([])

    if args.bench:
        from scripts.bench import main as bench_main

        return bench_main([])

    from jarvis.brain import last_session
    from jarvis.session import run, run_text

    resume = last_session(config.session_path) if args.resume else None
    if args.resume and resume is None:
        print("no previous conversation to continue; starting fresh.", file=sys.stderr)
    try:
        return asyncio.run(
            run_text(config, resume=resume) if args.text else run(config, resume=resume)
        )
    except KeyboardInterrupt:
        # asyncio.run cancels the main task and then re-raises. The shutdown has
        # already happened by this point; a traceback here would only be noise.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
