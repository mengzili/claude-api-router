"""Cross-platform process detachment + PID file tracking.

`claude-api-router start` uses this to spawn a second copy of itself as
a detached background process, then returns to the shell. `stop` and
`status` read the resulting PID file to find the running instance.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from claude_api_router.config import DEFAULT_CONFIG_DIR

PID_FILE = DEFAULT_CONFIG_DIR / "router.pid"
LOG_FILE = DEFAULT_CONFIG_DIR / "router.log"


def read_pid() -> int | None:
    try:
        raw = PID_FILE.read_text().strip()
    except FileNotFoundError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def write_pid(pid: int) -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))


def clear_pid() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def pid_alive(pid: int) -> bool:
    """Best-effort liveness check. On Windows we use OpenProcess; on
    Unix we send signal 0."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) on Windows actually sends a signal (not a
        # no-op liveness check), so we ask the OS directly.
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            # Couldn't read exit status — assume still alive.
            return True
        finally:
            kernel32.CloseHandle(h)
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Exists but not ours.
            return True


def running_pid() -> int | None:
    """Return the PID of a live router, or None. Cleans up stale file."""
    pid = read_pid()
    if pid is None:
        return None
    if not pid_alive(pid):
        clear_pid()
        return None
    return pid


def spawn_detached(argv: list[str]) -> int:
    """Spawn `argv` as a detached background process. Returns its PID."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_FILE, "ab", buffering=0)

    kwargs: dict = dict(
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **kwargs)
    log_fh.close()
    return proc.pid


def wait_for_admin(url: str, timeout: float = 8.0) -> bool:
    """Poll the admin URL until it responds with any HTTP status, or
    the timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except urllib.error.HTTPError:
            # A 4xx/5xx is still proof the server is up.
            return True
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            time.sleep(0.15)
    return False
