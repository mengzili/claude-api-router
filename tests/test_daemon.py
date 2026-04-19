import os
import subprocess
import sys
from pathlib import Path

from claude_api_router import daemon


def test_pid_alive_true_for_this_process():
    assert daemon.pid_alive(os.getpid()) is True


def test_pid_alive_false_for_nonexistent_pid():
    # Very unlikely PID to be alive.
    assert daemon.pid_alive(999_999_999) is False


def test_pid_file_roundtrip(monkeypatch, tmp_path: Path):
    pid_file = tmp_path / "router.pid"
    monkeypatch.setattr(daemon, "PID_FILE", pid_file)
    assert daemon.read_pid() is None
    daemon.write_pid(12345)
    assert daemon.read_pid() == 12345
    daemon.clear_pid()
    assert daemon.read_pid() is None
    # clear on missing file is a no-op
    daemon.clear_pid()


def test_running_pid_cleans_stale_file(monkeypatch, tmp_path: Path):
    pid_file = tmp_path / "router.pid"
    monkeypatch.setattr(daemon, "PID_FILE", pid_file)
    pid_file.write_text("999999999")
    assert daemon.running_pid() is None
    # Stale file removed.
    assert not pid_file.exists()


def test_running_pid_returns_live_pid(monkeypatch, tmp_path: Path):
    pid_file = tmp_path / "router.pid"
    monkeypatch.setattr(daemon, "PID_FILE", pid_file)
    pid_file.write_text(str(os.getpid()))
    assert daemon.running_pid() == os.getpid()


def test_spawn_detached_returns_pid(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(daemon, "LOG_FILE", tmp_path / "router.log")
    # Spawn a trivial child that exits quickly.
    argv = [sys.executable, "-c", "import time; time.sleep(0.1)"]
    pid = daemon.spawn_detached(argv)
    assert pid > 0
    # Reap it so the test doesn't leak a zombie on Unix.
    try:
        subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(0.3)"],
            timeout=1,
            check=False,
        )
    except Exception:
        pass
