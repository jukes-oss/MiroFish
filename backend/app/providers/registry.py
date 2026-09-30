"""Track the one in-flight CLI process so cancel can stop it."""

from __future__ import annotations

import os
import signal
import subprocess
import threading

_lock = threading.Lock()
_processes: dict[str, subprocess.Popen] = {}


def register(run_id: str, process: subprocess.Popen) -> None:
    with _lock:
        _processes[run_id] = process


def clear(run_id: str, process: subprocess.Popen | None = None) -> None:
    with _lock:
        current = _processes.get(run_id)
        if process is None or current is process:
            _processes.pop(run_id, None)


def terminate_run(run_id: str) -> None:
    with _lock:
        process = _processes.pop(run_id, None)
    if process is None or process.poll() is not None:
        return
    kill_process_group(process)


def kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return
