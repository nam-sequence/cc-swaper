"""Run Claude Code in a terminal and watch its lifecycle hook events."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import select
import shutil
import signal
import stat
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .process import run_cancelable
from .profiles import Profile


def _overrides_subscription(key: str) -> bool:
    """Strip inference credentials and routing from the parent shell.

    Claude adds variables over time. Prefixes fail closed for the Anthropic
    credential/routing family, while preserving unrelated development env vars.
    """
    return (
        key.startswith("ANTHROPIC_")
        or key.startswith("CLAUDE_CODE_OAUTH_")
        or key.startswith("CLAUDE_CODE_USE_")
        or key.startswith("CLAUDE_CODE_CLIENT_")
        or (key.startswith("CLAUDE_CODE_SKIP_") and key.endswith("_AUTH"))
        or key in {
            "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
            "AWS_BEARER_TOKEN_BEDROCK",
            "NODE_OPTIONS",
            "NODE_PATH",
            "NODE_TLS_REJECT_UNAUTHORIZED",
        }
    )

def claude_binary() -> str:
    override = os.environ.get("CC_SWAPER_CLAUDE_BIN")
    binary = override or shutil.which("claude")
    if not binary:
        raise RuntimeError("Claude Code is not installed or not on PATH")
    if not Path(binary).expanduser().is_absolute():
        raise RuntimeError("Claude Code executable must resolve from an absolute path")
    selected = Path(binary).expanduser()
    path = selected.resolve(strict=True)
    for parent in (selected.parent, path.parent):
        info = parent.stat()
        if info.st_uid not in (0, os.getuid()) or stat.S_IMODE(info.st_mode) & 0o022:
            raise RuntimeError(f"Claude Code executable directory is writable by others: {parent}")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"Claude Code executable is unavailable: {path}")
    info = path.stat()
    if info.st_uid not in (0, os.getuid()) or stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError(f"Claude Code executable is not trusted: {path}")
    return str(path)


def profile_environment(profile: Profile) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not _overrides_subscription(key)}
    env.pop("CLAUDE_CONFIG_DIR", None)
    if profile.config_dir is not None:
        if profile.config_dir.is_symlink() or not profile.config_dir.is_dir():
            raise RuntimeError(f"unsafe or missing profile directory: {profile.config_dir}")
        info = profile.config_dir.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError(f"profile directory is not private: {profile.config_dir}")
        env["CLAUDE_CONFIG_DIR"] = str(profile.config_dir)
    return env


def auth_details(
    profile: Profile, binary: str, *, cancel_event: threading.Event | None = None
) -> dict[str, object] | None:
    """Only ask Claude's supported status command; never read token files."""
    try:
        command = [binary, "auth", "status", "--json"]
        environment = profile_environment(profile)
        if cancel_event is None:
            result = subprocess.run(
                command, env=environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=10, check=False,
            )
        else:
            result = run_cancelable(command, environment, 10, cancel_event)
        status = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if not isinstance(status, dict):
        return None
    if result.returncode != 0 and status.get("loggedIn") is not False:
        return None
    return status


def auth_status(profile: Profile, binary: str) -> tuple[bool, str]:
    status = auth_details(profile, binary)
    if status is None:
        return False, "status unavailable"
    method = str(status.get("authMethod") or "none")
    # This tool is for separately signed-in claude.ai subscriptions. Refuse a
    # provider or API key so a selected profile cannot silently bill elsewhere.
    provider = status.get("apiProvider")
    return bool(
        status.get("loggedIn") and method == "claude.ai"
        and provider in (None, "firstParty")
    ), method


def auth_identity(profile: Profile, binary: str) -> tuple[str, str] | None:
    status = auth_details(profile, binary)
    if status is None or not status.get("loggedIn"):
        return None
    email = str(status.get("email") or "").strip().casefold()
    org_id = str(status.get("orgId") or "").strip()
    return (email, org_id) if email else None


@dataclass(frozen=True)
class RunResult:
    returncode: int
    exhausted: bool
    session_id: str | None = None
    transcript_path: Path | None = None


def _copy_winsize(source_fd: int, target_fd: int) -> None:
    try:
        size = fcntl.ioctl(source_fd, termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(target_fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _terminate_group(pid: int) -> int:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited:
            return status
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _, status = os.waitpid(pid, 0)
    return status


def _wait_for_transcript_quiet(path: Path | None) -> None:
    """Give Claude time to persist the failed turn before stopping the TUI.

    A hook can run while the transcript writer is still flushing. This is a
    bounded best-effort wait, not a guarantee that an external tool completed.
    """

    if path is None:
        return
    started = time.monotonic()
    changed = started
    previous: tuple[int, int] | None = None
    while time.monotonic() - started < 4:
        try:
            info = path.stat()
            current = (info.st_size, info.st_mtime_ns)
        except OSError:
            current = None
        if current != previous:
            previous = current
            changed = time.monotonic()
        if time.monotonic() - started >= 1 and time.monotonic() - changed >= 0.75:
            return
        time.sleep(0.1)


def run_interactive(
    binary: str,
    args: list[str],
    env: dict[str, str],
    *,
    monitor: Any = None,
    on_session_started: Callable[[], None] | None = None,
) -> RunResult:
    """Transparent PTY relay; only a quota hook can request a handoff."""
    input_fd = sys.stdin.fileno()
    output_fd = sys.stdout.fileno()
    if not (os.isatty(input_fd) and os.isatty(output_fd)):
        raise RuntimeError("automatic switching requires an interactive terminal")

    old_term = termios.tcgetattr(input_fd)
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvpe(binary, [binary, *args], env)
        except OSError as exc:
            os.write(2, f"Failed to start Claude Code: {exc}\n".encode())
            os._exit(127)

    _copy_winsize(input_fd, master)
    old_winch = signal.getsignal(signal.SIGWINCH)
    old_term_signal = signal.getsignal(signal.SIGTERM)
    old_hup_signal = signal.getsignal(signal.SIGHUP)
    child_status: int | None = None
    exhausted = False
    stop_signal: int | None = None

    def on_resize(_signum: int, _frame: object) -> None:
        _copy_winsize(input_fd, master)
        try:
            os.killpg(pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    def on_shutdown(signum: int, _frame: object) -> None:
        nonlocal stop_signal
        stop_signal = signum

    try:
        signal.signal(signal.SIGWINCH, on_resize)
        signal.signal(signal.SIGTERM, on_shutdown)
        signal.signal(signal.SIGHUP, on_shutdown)
        tty.setraw(input_fd)
        input_open = True
        while True:
            readers = [master]
            if input_open:
                readers.append(input_fd)
            ready, _, _ = select.select(readers, [], [], 0.2)
            if stop_signal is not None:
                child_status = _terminate_group(pid)
                break
            if input_fd in ready:
                chunk = os.read(input_fd, 65536)
                if chunk:
                    try:
                        os.write(master, chunk)
                    except OSError:
                        pass
                else:
                    input_open = False
            if master in ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                while chunk:
                    written = os.write(output_fd, chunk)
                    chunk = chunk[written:]
            if monitor is not None:
                monitor.poll()
                if on_session_started is not None and monitor.session_id is not None:
                    on_session_started()
                    on_session_started = None
            if monitor is not None and monitor.quota:
                _wait_for_transcript_quiet(monitor.transcript_path)
                child_status = _terminate_group(pid)
                exhausted = True
                break
            if child_status is None:
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    child_status = status
            if child_status is not None and master not in ready:
                break
    except BaseException:
        if child_status is None:
            try:
                _terminate_group(pid)
            except (OSError, ChildProcessError):
                pass
        raise
    finally:
        termios.tcsetattr(input_fd, termios.TCSADRAIN, old_term)
        signal.signal(signal.SIGWINCH, old_winch)
        signal.signal(signal.SIGTERM, old_term_signal)
        signal.signal(signal.SIGHUP, old_hup_signal)
        os.close(master)

    if stop_signal is not None:
        return RunResult(128 + stop_signal, False)
    if monitor is not None:
        monitor.poll()
        if on_session_started is not None and monitor.session_id is not None:
            on_session_started()
    exhausted = exhausted or (monitor is not None and monitor.quota)
    reported_id = monitor.session_id if monitor is not None else None
    reported_path = monitor.transcript_path if monitor is not None else None
    if exhausted:
        return RunResult(75, True, reported_id, reported_path)
    if child_status is None:
        _, child_status = os.waitpid(pid, 0)
    return RunResult(os.waitstatus_to_exitcode(child_status), False, reported_id, reported_path)


def run_passthrough(
    binary: str,
    args: list[str],
    env: dict[str, str],
    *,
    monitor: Any = None,
    on_session_started: Callable[[], None] | None = None,
) -> int:
    if monitor is None:
        return subprocess.call([binary, *args], env=env)

    process = subprocess.Popen([binary, *args], env=env)
    try:
        while True:
            monitor.poll()
            if on_session_started is not None and monitor.session_id is not None:
                on_session_started()
                on_session_started = None
            code = process.poll()
            if code is not None:
                return code
            time.sleep(0.1)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        raise
