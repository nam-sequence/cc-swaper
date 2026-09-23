"""Run a local child process with bounded timeout and prompt cancellation."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


class ProcessCancelled(RuntimeError):
    """Raised after an interrupted child process has been stopped."""


def run_cancelable(
    command: list[str], env: dict[str, str], timeout: float, cancel_event: threading.Event
) -> subprocess.CompletedProcess[str]:
    """Capture a command, stopping its process group on cancel or timeout."""

    if cancel_event.is_set():
        raise ProcessCancelled("process cancelled")
    process = subprocess.Popen(
        command, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=os.name == "posix",
    )
    deadline = time.monotonic() + timeout
    completed = False

    def stop_group(signum: int) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signum)
            elif process.poll() is None:
                process.send_signal(signum)
        except ProcessLookupError:
            pass

    try:
        while True:
            if cancel_event.is_set():
                raise ProcessCancelled("process cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                completed = True
                return subprocess.CompletedProcess(
                    command, process.returncode, stdout, stderr
                )
            except subprocess.TimeoutExpired:
                continue
    finally:
        if not completed:
            # The leader can exit while a helper still holds inherited pipes.
            # Kill the new-session group even if process.poll() says it exited.
            stop_group(signal.SIGTERM)
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                stop_group(signal.SIGKILL)
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    if process.stdout is not None:
                        process.stdout.close()
                    if process.stderr is not None:
                        process.stderr.close()
                    process.wait(timeout=1)
