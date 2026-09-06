"""
The lock that stops two JARVISes answering the same question. Worth testing
because the failure it prevents is invisible from inside either process: each
one's own log looks entirely correct, and only the room hears the echo.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from jarvis.config import Config
from jarvis.instance import AlreadyRunning, SingleInstance


def config_in(tmp_path) -> Config:
    return Config.from_env({"JARVIS_STATE_DIR": str(tmp_path)})


def test_a_second_instance_in_another_process_is_refused(tmp_path):
    """Two processes, because flock is per-process and re-locking would pass."""
    config = config_in(tmp_path)
    with SingleInstance(config) as first:
        assert first.path.read_text().strip() == str(os.getpid())
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(config.repo_root)!r})
            from jarvis.config import Config
            from jarvis.instance import AlreadyRunning, SingleInstance
            cfg = Config.from_env({{"JARVIS_STATE_DIR": {str(tmp_path)!r}}})
            try:
                SingleInstance(cfg).acquire()
            except AlreadyRunning as exc:
                print(f"refused:{{exc.pid}}")
            else:
                print("acquired")
        """)
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
    assert result.stdout.strip() == f"refused:{os.getpid()}", result.stderr


def test_the_lock_is_released_when_the_holder_lets_go(tmp_path):
    config = config_in(tmp_path)
    SingleInstance(config).acquire().release()
    with SingleInstance(config):  # must not raise
        pass


def test_the_error_names_the_process_to_stop():
    exc = AlreadyRunning(4321)
    assert "4321" in str(exc)
    assert exc.pid == 4321


def test_it_lives_outside_the_repo(tmp_path):
    config = config_in(tmp_path)
    assert SingleInstance(config).path.parent == config.state_dir


@pytest.mark.parametrize("junk", ["", "not-a-pid"])
def test_a_stale_lock_file_is_taken_over(tmp_path, junk):
    """A crash leaves the file behind; the kernel has already dropped the lock."""
    config = config_in(tmp_path)
    lock = SingleInstance(config)
    lock.path.write_text(junk)
    with lock:
        assert lock.path.read_text().strip() == str(os.getpid())
