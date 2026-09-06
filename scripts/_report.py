"""
The reporting half of `make doctor`: what a check result is, how it prints, and
the two ways this repo talks to the OS safely. Split out from `doctor.py` so
that file is only about *probing* the machine and this one is only about saying
what it found.

Every helper here has a timeout. `osascript` against an unresponsive app blocks
forever, and a diagnostic that hangs is worse than no diagnostic at all.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

OK, WARN, FAIL = "ok", "warn", "fail"

_MARK = {OK: "\033[32m✓\033[0m", WARN: "\033[33m!\033[0m", FAIL: "\033[31m✗\033[0m"}
_PLAIN = {OK: "+", WARN: "!", FAIL: "x"}

PRIVACY_PANE = "System Settings → Privacy & Security → {}"


@dataclass
class Check:
    section: str
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def run(cmd: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "not found")


def osascript(script: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return run(["osascript", "-e", script], timeout)


def last_line(text: str, fallback: str = "") -> str:
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return lines[-1] if lines else fallback


def with_timeout(fn: Callable[[], Any], seconds: float) -> tuple[bool, Any]:
    """Run a blocking call on a daemon thread so a wedged driver cannot hang us.

    Returns ``(finished, value_or_exception)``.
    """
    box: list[tuple[bool, Any]] = []
    thread = threading.Thread(target=lambda: box.append(_capture(fn)), daemon=True)
    thread.start()
    thread.join(seconds)
    if not box:
        return False, TimeoutError(f"no result in {seconds}s")
    return box[0]


def _capture(fn: Callable[[], Any]) -> tuple[bool, Any]:
    try:
        return True, fn()
    except BaseException as exc:  # noqa: BLE001 - reporting the failure is the job
        return False, exc


def render(checks: list[Check], tty: bool | None = None) -> str:
    marks = _MARK if (sys.stdout.isatty() if tty is None else tty) else _PLAIN
    lines: list[str] = []
    for section in dict.fromkeys(c.section for c in checks):
        lines.append(f"\n  {section}")
        for check in (c for c in checks if c.section == section):
            detail = f"  — {check.detail}" if check.detail else ""
            lines.append(f"    {marks[check.status]} {check.name}{detail}")
            if check.status != OK and check.fix:
                lines.append(f"        fix: {check.fix}")
    return "\n".join(lines)


def summarise(checks: list[Check]) -> dict[str, int]:
    return {status: sum(1 for c in checks if c.status == status) for status in (OK, WARN, FAIL)}
