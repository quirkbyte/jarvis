"""
The four macOS privacy grants JARVIS needs, each probed by doing the smallest
real version of the thing it protects. They are split out from `doctor.py`
because they are a different kind of check: the rest of the doctor asks what is
installed, and these ask what this Mac will let us do — which is the question
whose answer changes without any file changing.

macOS prompts for each of these once, at first use, and it is easy to miss the
dialog and never see it again. So every failure here names the exact Settings
pane, and says to restart the terminal, which is the step people skip.
"""

from __future__ import annotations

from pathlib import Path

from jarvis.config import CONFIG
from scripts._report import FAIL, OK, PRIVACY_PANE, WARN, Check, last_line, osascript, with_timeout


def check_microphone() -> Check:
    """Three failures wear the same face here, so separate them by hand.

    macOS does not raise when microphone permission is denied — it opens the
    stream and hands you zeros. A Bluetooth mic *also* hands you zeros for its
    first tenth of a second, while the profile switches to one that has a
    microphone at all. So: listen past the warm-up, then judge, and treat real
    silence as the failure it is rather than a curiosity.
    """
    audio = CONFIG.audio

    def probe() -> tuple[str, float, int]:
        import numpy as np
        import sounddevice as sd

        inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        if not inputs:
            raise LookupError("no input device is attached to this Mac")
        device = sd.query_devices(kind="input")
        frames = int(audio.sample_rate * audio.probe_ms / 1000)
        warmup = int(audio.sample_rate * audio.probe_warmup_ms / 1000)
        with sd.InputStream(
            samplerate=audio.sample_rate, channels=1, dtype="int16", blocksize=1600
        ) as stream:
            captured, _ = stream.read(frames)
        peak = int(np.abs(captured[warmup:]).max())
        return str(device["name"]), float(device["default_samplerate"]), peak

    finished, result = with_timeout(probe, 15.0)
    if not finished and isinstance(result, LookupError):
        return Check(
            "permissions",
            "microphone",
            FAIL,
            str(result),
            fix="plug in a USB mic or a webcam, or connect a headset, then pick it in "
            "System Settings → Sound → Input (a Mac mini has no built-in microphone)",
        )
    if not finished:
        return Check(
            "permissions",
            "microphone",
            FAIL,
            f"{type(result).__name__}: {result}",
            fix=PRIVACY_PANE.format("Microphone")
            + " — enable your terminal, then quit and reopen it",
        )

    name, rate, peak = result
    listened = audio.probe_ms - audio.probe_warmup_ms
    where = f"{name} @ {rate / 1000:.0f}kHz"
    if peak == 0:
        return Check(
            "permissions",
            "microphone",
            FAIL,
            f"{where}: {listened}ms of digital silence",
            # This is the one that catches people: a denied mic is not an error.
            fix=PRIVACY_PANE.format("Microphone")
            + " — macOS returns silence rather than failing when the grant is "
            "missing; enable your terminal, quit it and reopen it. If it is already "
            "enabled, the wrong device is selected in System Settings → Sound → Input",
        )
    if peak < audio.probe_min_peak:
        return Check(
            "permissions",
            "microphone",
            WARN,
            f"{where}: signal present but very faint (peak {peak}/32767)",
            fix="raise the input level in System Settings → Sound → Input",
        )
    return Check("permissions", "microphone", OK, f"{where}: {listened}ms heard, peak {peak}")


def check_automation() -> Check:
    proc = osascript('tell application "System Events" to get name')
    if proc.returncode == 0:
        return Check("permissions", "automation", OK, proc.stdout.strip())
    return Check(
        "permissions",
        "automation",
        FAIL,
        last_line(proc.stderr, "osascript failed"),
        fix=PRIVACY_PANE.format("Automation") + " — allow your terminal to control System "
        "Events (Finder, Music and Messages prompt separately, on first use)",
    )


def check_accessibility() -> Check:
    """`UI elements enabled` answers this outright.

    Reading a window position needs there to *be* a front window, so a machine
    with nothing open looks identical to one that has refused the grant. This
    property does not, and it is what window management in phase 4 depends on.
    """
    probe = osascript('tell application "System Events" to get UI elements enabled')
    if probe.returncode == 0 and probe.stdout.strip().lower() == "true":
        window = osascript(
            'tell application "System Events" to tell (first application process whose '
            "frontmost is true) to get position of window 1"
        )
        where = f"front window at {window.stdout.strip()}" if window.returncode == 0 else "granted"
        return Check("permissions", "accessibility", OK, where)
    detail = last_line(probe.stderr) or "assistive access is off for this terminal"
    return Check(
        "permissions",
        "accessibility",
        FAIL,
        detail,
        fix=PRIVACY_PANE.format("Accessibility") + " — add your terminal, then quit and reopen it",
    )


def check_full_disk_access() -> Check:
    db = Path("~/Library/Messages/chat.db").expanduser()
    if not db.exists():
        return Check(
            "permissions",
            "full disk access",
            WARN,
            "no chat.db on this Mac",
            fix="only Messages needs it; grant it when phase 4 lands",
        )
    try:
        with db.open("rb") as fh:
            fh.read(16)
        return Check("permissions", "full disk access", OK, "chat.db readable")
    except PermissionError:
        return Check(
            "permissions",
            "full disk access",
            FAIL,
            "chat.db unreadable",
            fix=PRIVACY_PANE.format("Full Disk Access") + " — add your terminal, "
            "then quit and reopen it",
        )
    except OSError as exc:
        return Check("permissions", "full disk access", WARN, str(exc))
