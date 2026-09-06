"""
The script you run when JARVIS will not start. Everything it checks is something
with an unhelpful failure mode later: a missing portaudio surfaces as a segfault,
a missing Automation grant as a bare AppleScript error number, a missing Full
Disk Access as an empty Messages database. So every red line carries the exact
command or Settings pane that fixes it — a checklist that tells you what is
wrong but not what to do is just anxiety.

Exit status is 1 if anything failed, 0 if the worst of it is a warning.
"""

from __future__ import annotations

import importlib
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from jarvis.config import load_dotenv  # noqa: E402
from scripts._report import FAIL, OK, WARN, Check, render, run, summarise  # noqa: E402
from scripts.permissions import (  # noqa: E402
    check_accessibility,
    check_automation,
    check_full_disk_access,
    check_microphone,
)

# (pip name, import name, requirement, why it is here). requirement: True =
# always, "silicon"/"intel" = on that machine only, False = optional.
DEPS: list[tuple[str, str, Any, str]] = [
    ("numpy", "numpy", True, "audio frames"),
    ("sounddevice", "sounddevice", True, "microphone and speakers"),
    ("openwakeword", "openwakeword", True, "hey_jarvis"),
    ("onnxruntime", "onnxruntime", True, "wake word inference"),
    ("webrtcvad-wheels", "webrtcvad", True, "endpointing"),
    ("parakeet-mlx", "parakeet_mlx", "silicon", "speech to text"),
    ("faster-whisper", "faster_whisper", "intel", "speech to text fallback"),
    ("claude-agent-sdk", "claude_agent_sdk", True, "the agent loop"),
    ("elevenlabs", "elevenlabs", False, "the British voice; `say` is the fallback"),
    ("fastapi", "fastapi", True, "HUD transport"),
    ("uvicorn", "uvicorn", True, "HUD transport"),
    ("pytest", "pytest", True, "make test"),
]

OPTIONAL_TOOLS = [
    ("blueutil", "bluetooth on and off"),
    ("brightness", "display brightness"),
    ("icalBuddy", "fast calendar reads"),
    ("yabai", "window management"),
    ("nowplaying-cli", "what is playing, in any app"),
    ("displayplacer", "display arrangement"),
]

IS_SILICON = platform.machine() == "arm64"
THIS_ARCH = "silicon" if IS_SILICON else "intel"
IS_MACOS = platform.system() == "Darwin"


def check_runtime() -> list[Check]:
    v = sys.version_info
    state = Path(os.environ.get("JARVIS_STATE_DIR", "~/.jarvis")).expanduser()
    return [
        Check(
            "runtime",
            f"python {v.major}.{v.minor}.{v.micro}",
            OK if (v.major, v.minor) >= (3, 11) else FAIL,
            "" if (v.major, v.minor) >= (3, 11) else "3.11 or newer required",
            "brew install python@3.12 && make PYTHON=$(brew --prefix)/bin/python3.12 install",
        ),
        Check(
            "runtime",
            f"macos {platform.mac_ver()[0] or '—'}",
            OK if IS_MACOS else FAIL,
            fix="JARVIS drives a Mac; there is no other supported target.",
        ),
        Check(
            "runtime",
            f"cpu {platform.machine()}",
            OK if IS_SILICON else WARN,
            "apple silicon — parakeet-mlx runs on the GPU"
            if IS_SILICON
            else "intel — speech to text falls back to faster-whisper and is slower",
            fix="" if IS_SILICON else "nothing to fix; expect ~2x the STT latency budget",
        ),
        Check(
            "runtime",
            f"state dir {state}",
            OK if state.is_dir() and os.access(state, os.W_OK) else FAIL,
            "memory and logs live here, outside the repo",
            fix=f"mkdir -p {state} && chmod u+rwx {state}",
        ),
        check_running(),
    ]


def check_running() -> Check:
    """Is one already listening? Two copies sound exactly like an echo."""
    from jarvis.config import CONFIG
    from jarvis.instance import AlreadyRunning, SingleInstance

    lock = SingleInstance(CONFIG)
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        return Check(
            "runtime",
            "not already running",
            WARN,
            f"jarvis is live as process {exc.pid}",
            fix=f"that is expected if you meant to leave it up; otherwise: kill {exc.pid}",
        )
    lock.release()
    return Check("runtime", "not already running", OK, "no other instance holds the lock")


def check_toolchain() -> list[Check]:
    brew = shutil.which("brew")
    checks = [
        Check(
            "toolchain",
            "homebrew",
            OK if brew else FAIL,
            brew or "not on PATH",
            fix='/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew'
            '/install/HEAD/install.sh)"',
        )
    ]
    if brew:
        proc = run([brew, "--prefix", "portaudio"], timeout=30)
        found = proc.returncode == 0
        checks.append(
            Check(
                "toolchain",
                "portaudio",
                OK if found else FAIL,
                proc.stdout.strip() if found else "sounddevice cannot open a stream without it",
                fix="brew install portaudio",
            )
        )
    return checks


def check_deps() -> list[Check]:
    checks: list[Check] = []
    for pip_name, module, requirement, why in DEPS:
        if requirement in ("silicon", "intel") and requirement != THIS_ARCH:
            continue  # the other architecture's backend; not installed, not wanted
        needed = requirement is not False
        try:
            importlib.import_module(module)
            checks.append(Check("dependencies", pip_name, OK, why))
        except Exception as exc:  # noqa: BLE001 - an arch-mismatched wheel is a failure too
            checks.append(
                Check(
                    "dependencies",
                    pip_name,
                    FAIL if needed else WARN,
                    f"{why} — {type(exc).__name__}: {exc}",
                    fix=f"make install   (or: pip install {pip_name})",
                )
            )
    checks.append(check_wake_model())
    return checks


def check_wake_model() -> Check:
    """openWakeWord ships the model list but downloads the weights on first use."""
    download = (
        'make install   (or: python -c "import openwakeword.utils as u; u.download_models()")'
    )
    try:
        import openwakeword

        models = Path(openwakeword.__file__).parent / "resources" / "models"
        wake = sorted(models.glob("hey_jarvis*.onnx"))
        melspec = sorted(models.glob("melspectrogram*.onnx"))
        embedding = sorted(models.glob("embedding_model*.onnx"))
        if wake and melspec and embedding:
            return Check("dependencies", "hey_jarvis model", OK, wake[0].name)
        missing = "weights not downloaded" if not wake else "feature models missing"
        return Check("dependencies", "hey_jarvis model", FAIL, missing, fix=download)
    except Exception as exc:  # noqa: BLE001
        return Check("dependencies", "hey_jarvis model", FAIL, str(exc), fix=download)


def check_credentials() -> list[Check]:
    claude_login = any(
        p.expanduser().exists()
        for p in (Path("~/.claude/.credentials.json"), Path("~/.claude.json"))
    )
    if os.environ.get("ANTHROPIC_API_KEY"):
        how = "ANTHROPIC_API_KEY is set"
    elif claude_login:
        how = "reusing the Claude Code login"
    else:
        how = ""
    key, voice = os.environ.get("ELEVENLABS_API_KEY"), os.environ.get("ELEVENLABS_VOICE_ID")
    return [
        Check(
            "credentials",
            "anthropic auth",
            OK if how else FAIL,
            how or "no key, no login",
            fix="run `claude` and log in, or put ANTHROPIC_API_KEY in .env",
        ),
        Check(
            "credentials",
            "elevenlabs key",
            OK if key else WARN,
            "voice out" if key else "not set — `say -v Daniel` is the fallback and works offline",
            fix="" if key else "optional: put ELEVENLABS_API_KEY in .env",
        ),
        Check(
            "credentials",
            "elevenlabs voice",
            OK if voice else WARN,
            voice or "no voice chosen yet",
            fix="" if voice else "optional: phase 2 adds scripts/voices.py to audition and pick",
        ),
    ]


def check_optional_tools() -> list[Check]:
    return [
        Check(
            "optional tools",
            tool,
            OK if shutil.which(tool) else WARN,
            why if shutil.which(tool) else f"{why} — not installed",
            fix="" if shutil.which(tool) else f"brew install {tool}",
        )
        for tool, why in OPTIONAL_TOOLS
    ]


def collect(skip_permissions: bool = False) -> list[Check]:
    checks = check_runtime() + check_toolchain() + check_deps() + check_credentials()
    if IS_MACOS and not skip_permissions:
        checks += [
            check_microphone(),
            check_automation(),
            check_accessibility(),
            check_full_disk_access(),
        ]
    return checks + check_optional_tools()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    # Read .env the way jarvis does, or the doctor reports a different machine
    # from the one the assistant will actually run on.
    load_dotenv()
    checks = collect(skip_permissions="--no-permissions" in args)
    print(
        f"\nJARVIS doctor — {platform.platform(terse=True)} · "
        f"python {'.'.join(map(str, sys.version_info[:3]))}\n  {sys.executable}"
    )
    print(render(checks))
    counts = summarise(checks)
    print(f"\n  {counts[OK]} ok · {counts[WARN]} warning · {counts[FAIL]} failing\n")
    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    raise SystemExit(main())
