"""Finish Claude's per-profile TUI onboarding after a successful CLI login.

Claude may report an account as signed in while its interactive TUI still
shows the login wizard. The wizard uses a marker in that profile's private
``.claude.json``; ``claude auth login`` does not always write it.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .profiles import Profile
from .runner import auth_details


_MAX_STATE_BYTES = 8 * 1024 * 1024


def needs_repair(profile: Profile) -> bool:
    """Return whether an existing managed profile state lacks onboarding."""

    if profile.config_dir is None:
        return False
    state = _read_state(profile.config_dir / ".claude.json")
    if state is None:
        return False
    marker = state[0].get("hasCompletedOnboarding")
    if marker is True:
        return False
    if marker is None or marker is False:
        return True
    raise ValueError("invalid Claude onboarding marker")


def repair_if_authenticated(profile: Profile, binary: str) -> bool:
    """Write only the missing marker after Claude confirms this login.

    The caller holds the exclusive ccs profile lock. Other Claude processes
    may not honor it, so re-read and compare the state before replacing it.
    """

    if profile.config_dir is None:
        return False
    path = profile.config_dir / ".claude.json"
    state = _read_state(path)
    if state is None:
        return False
    payload, original, info = state
    marker = payload.get("hasCompletedOnboarding")
    if marker is True:
        return False
    if marker is not None and marker is not False:
        raise ValueError("invalid Claude onboarding marker")

    details = auth_details(profile, binary)
    if not details or not (
        details.get("loggedIn") is True
        and details.get("authMethod") == "claude.ai"
        and details.get("apiProvider") == "firstParty"
        and details.get("configDirectory") == str(profile.config_dir)
    ):
        return False

    updated = _with_marker(payload, original)
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".claude.json.ccs-", dir=path.parent)
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())

        current = _read_state(path)
        if current is None:
            raise RuntimeError("Claude profile state changed during onboarding repair")
        if (
            current[1] != original
            or current[2].st_dev != info.st_dev
            or current[2].st_ino != info.st_ino
        ):
            raise RuntimeError("Claude profile state changed during onboarding repair")
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_state(path: Path) -> tuple[dict[str, Any], bytes, os.stat_result] | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("unsafe Claude profile state file") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > _MAX_STATE_BYTES
        ):
            raise ValueError("unsafe Claude profile state file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, _MAX_STATE_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_STATE_BYTES:
                raise ValueError("Claude profile state file is too large")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("invalid Claude profile state JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Claude profile state must be a JSON object")
    return payload, raw, info


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _with_marker(payload: dict[str, Any], raw: bytes) -> bytes:
    if "hasCompletedOnboarding" in payload:
        updated = dict(payload)
        updated["hasCompletedOnboarding"] = True
        return (json.dumps(updated, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    stripped = raw.rstrip()
    if not stripped.endswith(b"}"):
        raise ValueError("invalid Claude profile state JSON")
    insertion = b'"hasCompletedOnboarding":true'
    if payload:
        insertion = b"," + insertion
    return stripped[:-1] + insertion + b"}" + raw[len(stripped):]


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
