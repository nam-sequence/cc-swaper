"""macOS LaunchAgent that continuously inventories cc-swaper sessions."""

from __future__ import annotations

import json
import os
import plistlib
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .profiles import ProfileStore
from .shell import _trusted_ccs_path


LABEL = "com.cc-swaper.monitor"
POLL_SECONDS = 3


def _require_macos() -> None:
    if os.uname().sysname != "Darwin":
        raise RuntimeError("the always-on service currently supports macOS LaunchAgents")


def _launchctl() -> str:
    binary = Path("/bin/launchctl")
    if not binary.is_file() or binary.stat().st_uid != 0 or not os.access(binary, os.X_OK):
        raise RuntimeError("launchctl is unavailable")
    return str(binary)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _status_path(store: ProfileStore) -> Path:
    return store.home / "monitor.json"


def _atomic_bytes(path: Path, data: bytes, mode: int) -> None:
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _desired_plist(store: ProfileStore, *, socket_override: str | None = None) -> dict[str, Any]:
    ccs = _trusted_ccs_path()
    socket = socket_override or os.environ.get("CC_SWAPER_TMUX_SOCKET", "cc-swaper")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", socket):
        raise ValueError("CC_SWAPER_TMUX_SOCKET has an invalid name")
    return {
        "Label": LABEL,
        "ProgramArguments": [str(ccs), "service", "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "CC_SWAPER_HOME": str(store.home),
            "CC_SWAPER_TMUX_SOCKET": socket,
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
        "StandardOutPath": str(store.home / "monitor.stdout.log"),
        "StandardErrorPath": str(store.home / "monitor.stderr.log"),
    }


def _loaded() -> bool:
    result = subprocess.run(
        [_launchctl(), "print", f"{_domain()}/{LABEL}"],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def install(store: ProfileStore) -> Path:
    _require_macos()
    plist = _plist_path()
    if plist.is_symlink() or plist.parent.is_symlink():
        raise RuntimeError(f"refusing symlinked LaunchAgent plist: {plist}")
    plist.parent.mkdir(parents=True, exist_ok=True)
    parent = plist.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022:
        raise RuntimeError(f"LaunchAgents directory is writable by others: {plist.parent}")
    for name in ("monitor.stdout.log", "monitor.stderr.log"):
        path = store.home / name
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError(f"untrusted monitor log: {path}")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    desired = _desired_plist(store)
    desired_bytes = plistlib.dumps(desired)
    loaded_before = _loaded()
    old_bytes: bytes | None = None
    changed = False
    if plist.exists():
        info = plist.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise RuntimeError(f"untrusted LaunchAgent plist: {plist}")
        old_bytes = plist.read_bytes()
        current = plistlib.loads(old_bytes)
        if not isinstance(current, dict) or not isinstance(current.get("EnvironmentVariables"), dict):
            raise RuntimeError("invalid cc-swaper LaunchAgent plist")
        if current.get("Label") != LABEL or current["EnvironmentVariables"].get("CC_SWAPER_HOME") != str(store.home):
            raise RuntimeError("LaunchAgent belongs to another cc-swaper store")
        if current != desired:
            if loaded_before:
                stopped = subprocess.run(
                    [_launchctl(), "bootout", _domain(), str(plist)],
                    capture_output=True, text=True, check=False,
                )
                if stopped.returncode != 0:
                    raise RuntimeError(f"could not stop old LaunchAgent: {stopped.stderr.strip()}")
            _atomic_bytes(plist, desired_bytes, 0o644)
            changed = True
    else:
        if loaded_before:
            raise RuntimeError("a LaunchAgent with this label is loaded from another path")
        _atomic_bytes(plist, desired_bytes, 0o644)
        changed = True
    if changed or not loaded_before:
        result = subprocess.run(
            [_launchctl(), "bootstrap", _domain(), str(plist)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            if old_bytes is not None:
                _atomic_bytes(plist, old_bytes, 0o644)
                if loaded_before:
                    subprocess.run(
                        [_launchctl(), "bootstrap", _domain(), str(plist)],
                        capture_output=True, text=True, check=False,
                    )
            else:
                plist.unlink(missing_ok=True)
            raise RuntimeError(f"could not start LaunchAgent: {result.stderr.strip()}")
    else:
        result = subprocess.run(
            [_launchctl(), "kickstart", "-k", f"{_domain()}/{LABEL}"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"could not restart LaunchAgent: {result.stderr.strip()}")
    return plist


def uninstall(store: ProfileStore) -> Path:
    _require_macos()
    plist = _plist_path()
    if not plist.exists():
        return plist
    if plist.is_symlink() or plist.parent.is_symlink():
        raise RuntimeError(f"refusing symlinked LaunchAgent plist: {plist}")
    info = plist.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError(f"untrusted LaunchAgent plist: {plist}")
    current = plistlib.loads(plist.read_bytes())
    if not isinstance(current, dict) or not isinstance(current.get("EnvironmentVariables"), dict):
        raise RuntimeError("invalid cc-swaper LaunchAgent plist")
    socket = current["EnvironmentVariables"].get("CC_SWAPER_TMUX_SOCKET")
    if not isinstance(socket, str) or current != _desired_plist(store, socket_override=socket):
        raise RuntimeError("LaunchAgent differs from the current cc-swaper configuration; run 'ccs service install' first")
    if _loaded():
        result = subprocess.run(
            [_launchctl(), "bootout", _domain(), str(plist)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"could not stop LaunchAgent: {result.stderr.strip()}")
    plist.unlink()
    return plist


def _write_snapshot(store: ProfileStore, snapshot: dict[str, Any]) -> None:
    data = (json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    _atomic_bytes(_status_path(store), data, 0o600)


def _snapshot(store: ProfileStore) -> dict[str, Any]:
    from .tmux_sessions import TmuxSessions

    sessions = []
    running_script = Path(sys.argv[0])
    ccs_binary = (
        running_script if running_script.is_absolute() and running_script.name == "ccs"
        else _trusted_ccs_path()
    )
    for item in TmuxSessions(store, ccs_binary=ccs_binary).list_sessions():
        cwd = Path(str(item.get("cwd") or ""))
        record = store.last_transcript(cwd) if cwd.is_absolute() else None
        sessions.append({
            "name": item.get("name"),
            "cwd": str(cwd) if cwd.is_absolute() else None,
            "attached": item.get("attached", False),
            "dead": item.get("dead", False),
            "profile": record[0] if record else item.get("initial_profile"),
            "session_id": store.last_session(cwd) if cwd.is_absolute() else None,
        })
    return {"pid": os.getpid(), "updated_at": time.time(), "sessions": sessions}


def run(store: ProfileStore) -> int:
    _require_macos()
    stopping = threading.Event()
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)

    def shutdown(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        while not stopping.is_set():
            try:
                _write_snapshot(store, _snapshot(store))
            except Exception as exc:
                _write_snapshot(store, {"pid": os.getpid(), "updated_at": time.time(), "sessions": [], "error": str(exc)})
            stopping.wait(POLL_SECONDS)
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
    return 0


def status(store: ProfileStore) -> dict[str, Any]:
    _require_macos()
    loaded = _loaded()
    try:
        snapshot = json.loads(_status_path(store).read_text())
    except (OSError, json.JSONDecodeError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    age = time.time() - snapshot.get("updated_at", 0) if isinstance(snapshot.get("updated_at"), (int, float)) else None
    return {
        "loaded": loaded,
        "healthy": bool(loaded and age is not None and 0 <= age < POLL_SECONDS * 3 and "error" not in snapshot),
        "age_seconds": age,
        "sessions": snapshot.get("sessions", []) if isinstance(snapshot, dict) else [],
        "error": snapshot.get("error") if isinstance(snapshot, dict) else None,
    }
