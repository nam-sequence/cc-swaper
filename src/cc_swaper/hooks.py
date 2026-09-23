"""Small, per-run Claude hooks that report session identity and quota failure.

The hook never prints its input. Its private event file contains only the
session UUID and transcript path, not messages or credentials.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

LIMIT_PATTERNS = (
    re.compile(
        r"you['’]?ve hit your (?:(?:\d+-hour|weekly) )?(?:usage )?limit\s*[·•∙\-:]\s*resets",
        re.I,
    ),
    re.compile(r"claude ai usage limit reached,?\s*please try again after", re.I),
    re.compile(r"usage limit reached\s*[·•∙\-:]\s*resets", re.I),
    re.compile(
        r"(?:\d+[- ]hour|weekly|monthly) limit reached\s*[·•∙\-:]\s*resets",
        re.I,
    ),
    re.compile(r"this request would exceed your account['’]s rate limit", re.I),
)


def hook_settings(event_file: Path) -> str:
    handler = {
        "type": "command",
        "command": sys.executable,
        "args": ["-I", str(Path(__file__).resolve()), str(event_file)],
        "timeout": 5,
    }
    return json.dumps(
        {
            "hooks": {
                "SessionStart": [{"hooks": [handler]}],
                "StopFailure": [{"matcher": "rate_limit", "hooks": [handler]}],
            }
        },
        separators=(",", ":"),
    )


def _valid_identity(payload: dict[str, Any]) -> tuple[str, str] | None:
    raw_id = payload.get("session_id")
    raw_path = payload.get("transcript_path")
    if not isinstance(raw_id, str) or not isinstance(raw_path, str):
        return None
    try:
        session_id = str(uuid.UUID(raw_id))
    except ValueError:
        return None
    path = Path(raw_path)
    if not path.is_absolute() or "\x00" in raw_path:
        return None
    return session_id, raw_path


def minimal_event(payload: dict[str, Any]) -> dict[str, str] | None:
    if payload.get("agent_id"):
        return None
    identity = _valid_identity(payload)
    if identity is None:
        return None
    session_id, transcript_path = identity
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        return {"type": "start", "session_id": session_id, "transcript_path": transcript_path}
    if event != "StopFailure" or payload.get("error") != "rate_limit":
        return None
    details = "\n".join(
        str(payload.get(key) or "")
        for key in ("error_details", "last_assistant_message")
    )
    if not any(pattern.search(details) for pattern in LIMIT_PATTERNS):
        return None
    return {"type": "limit", "session_id": session_id, "transcript_path": transcript_path}


def write_event(event_file: Path, event: dict[str, str]) -> None:
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(event_file, flags)
    try:
        data = (json.dumps(event, separators=(",", ":")) + "\n").encode()
        while data:
            data = data[os.write(fd, data) :]
    finally:
        os.close(fd)


class HookMonitor:
    def __init__(
        self,
        event_file: Path,
        *,
        profile_projects: Path | None = None,
        expected_session_id: str | None = None,
    ):
        self.event_file = event_file
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        self._fd = os.open(event_file, flags)
        info = os.fstat(self._fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(self._fd)
            raise ValueError("unsafe hook event file")
        self.profile_projects = profile_projects.resolve() if profile_projects else None
        self.expected_session_id = expected_session_id
        self._offset = 0
        self._pending = b""
        self.session_id: str | None = None
        self.transcript_path: Path | None = None
        self.quota = False

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> "HookMonitor":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def _valid_transcript(
        self, path: Path, session_id: str, *, require_file: bool
    ) -> bool:
        if path.suffix != ".jsonl" or path.stem != session_id:
            return False
        if path.is_symlink() or path.parent.is_symlink():
            return False
        if self.profile_projects is not None:
            parent = path.parent.resolve(strict=False)
            if parent.parent != self.profile_projects:
                return False
        if not require_file:
            return True
        try:
            info = path.stat()
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()

    def poll(self) -> None:
        info = os.fstat(self._fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1_000_000:
            return
        os.lseek(self._fd, self._offset, os.SEEK_SET)
        data = os.read(self._fd, 65536)
        self._offset += len(data)
        self._pending += data
        if len(self._pending) > 65536:
            self._pending = b""
            return
        while b"\n" in self._pending:
            line, self._pending = self._pending.split(b"\n", 1)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            identity = _valid_identity(event)
            if identity is None:
                continue
            session_id, transcript_path = identity
            path = Path(transcript_path)
            if event.get("type") == "start":
                if self.session_id is None and self.expected_session_id is not None:
                    if session_id != self.expected_session_id:
                        continue
                if not self._valid_transcript(path, session_id, require_file=False):
                    continue
                self.session_id = session_id
                self.transcript_path = path
            elif event.get("type") == "limit":
                if (
                    session_id != self.session_id
                    or path != self.transcript_path
                    or not self._valid_transcript(path, session_id, require_file=True)
                ):
                    continue
                self.quota = True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        return 0
    try:
        payload = json.loads(sys.stdin.read(1_000_000))
        if not isinstance(payload, dict):
            return 0
        event = minimal_event(payload)
        if event is not None:
            write_event(Path(argv[0]), event)
    except (OSError, ValueError, json.JSONDecodeError):
        # A hook is advisory. An unavailable event file must never block Claude.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
