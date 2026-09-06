"""
One JARVIS at a time.

Two copies both hold the microphone open, both hear the wake word, and both
answer — which sounds exactly like an echo and is very hard to diagnose from the
inside, because each process's own log looks perfectly correct. An always-
listening program has no business starting twice, so it refuses.

The lock is an flock on a file in the state directory: the kernel releases it
when the process dies, however it dies, so a crash never leaves a stale lock
that needs clearing by hand.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from jarvis.config import CONFIG, Config


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        who = f"process {pid}" if pid else "another process"
        super().__init__(f"jarvis is already running ({who})")


class SingleInstance:
    """Hold the run lock for as long as this object is open."""

    def __init__(self, config: Config = CONFIG, name: str = "jarvis.pid") -> None:
        self.path: Path = config.state_dir / name
        self._handle = None

    def acquire(self) -> SingleInstance:
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.seek(0)
            existing = handle.read().strip()
            handle.close()
            raise AlreadyRunning(int(existing) if existing.isdigit() else None) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> SingleInstance:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()
