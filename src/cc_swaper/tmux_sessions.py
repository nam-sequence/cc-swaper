"""Manage persistent tmux sessions for Claude project invocations.

The background runner intentionally keeps tmux concerns in a small adapter.
The command inside a session is still ``ccs`` in foreground mode, so the
normal PTY based quota supervisor remains responsible for account switching.
This module only creates, discovers, attaches to, and stops those sessions.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any, Sequence

from .profiles import ProfileStore


_DEFAULT_SOCKET = "cc-swaper"
_SOCKET_ENV = "CC_SWAPER_TMUX_SOCKET"
_CLAUDE_ENV = "CC_SWAPER_CLAUDE_BIN"
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_SESSION_SLUG = re.compile(r"[^a-z0-9]+")
_SOCKET_NAME = re.compile(r"[A-Za-z0-9_-]+\Z")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_RUN_ID = re.compile(r"[0-9a-f]{12}\Z")
_UNSAFE_TMUX_ENV = (
    "ANTHROPIC_",
    "CLAUDE_CODE_",
    "AWS_",
    "DYLD_",
    "LD_",
    "PYTHONPATH",
    "PYTHONHOME",
)
_UNSAFE_TMUX_ENV_EXACT = frozenset(
    {
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CONFIG_DIR",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "BASH_ENV",
        "ENV",
        "SHELL",
        "ZDOTDIR",
        "TMUX_TMPDIR",
        "CC_SWAPER_HOME",
        "CC_SWAPER_RUN_ID",
        "CC_SWAPER_TMUX_SOCKET",
    }
)


def _canonical_cwd(cwd: Path) -> Path:
    """Return the canonical project path without following a missing leaf."""

    return Path(cwd).expanduser().resolve(strict=False)


def _project_slug(cwd: Path) -> str:
    basename = cwd.name.lower() or "root"
    slug = _SESSION_SLUG.sub("-", basename).strip("-")
    return (slug or "project")[:48]


def session_name(cwd: Path) -> str:
    """Return the legacy stable tmux name for a canonical project path.

    The short readable portion helps users identify a session in ``tmux``;
    the path digest keeps projects with the same basename distinct.
    """

    canonical = _canonical_cwd(cwd)
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:12]
    return f"ccs-{_project_slug(canonical)}-{digest}"


def _uuid_value(value: str, label: str) -> str:
    """Validate and normalize a canonical UUID used in managed metadata."""

    if not isinstance(value, str) or not _UUID.fullmatch(value.lower()):
        raise ValueError(f"{label} must be a UUID")
    return value.lower()


def _managed_session_name(cwd: Path, run_id: str) -> str:
    return f"{session_name(cwd)}-{run_id}"


def _trusted_executable(candidate: Path | str | None, *, label: str, lookup: str) -> Path:
    """Resolve an executable and reject relative or writable code paths."""

    raw = str(candidate) if candidate is not None else shutil.which(lookup)
    if not raw:
        raise RuntimeError(f"{label} is not installed or not on PATH")
    selected = Path(raw).expanduser()
    if not selected.is_absolute():
        raise RuntimeError(f"{label} executable must resolve from an absolute path")
    try:
        resolved = selected.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise RuntimeError(f"{label} executable is unavailable: {selected}") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise RuntimeError(f"{label} executable is unavailable: {resolved}")

    uid = os.getuid()
    for parent in (selected.parent, resolved.parent):
        try:
            parent_info = parent.stat()
        except OSError as exc:
            raise RuntimeError(f"{label} executable directory is unavailable: {parent}") from exc
        parent_mode = stat.S_IMODE(parent_info.st_mode)
        if parent_info.st_uid == uid:
            # Homebrew commonly uses a user-owned group-writable prefix.  A
            # world-writable directory is still never trusted.
            unsafe_parent = bool(parent_mode & 0o002)
        else:
            unsafe_parent = parent_info.st_uid != 0 or bool(parent_mode & 0o022)
        if unsafe_parent:
            raise RuntimeError(f"{label} executable directory is writable by others: {parent}")
    if info.st_uid not in (0, uid) or stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError(f"{label} executable is not trusted: {resolved}")
    return resolved


def _status_output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "").strip()


class TmuxSessions:
    """Operate project sessions on the private ``cc-swaper`` tmux socket."""

    def __init__(
        self,
        store: ProfileStore,
        ccs_binary: Path | None = None,
        tmux_binary: Path | None = None,
    ) -> None:
        self.store = store
        self.ccs_binary = _trusted_executable(ccs_binary, label="ccs", lookup="ccs")
        self.tmux_binary = _trusted_executable(tmux_binary, label="tmux", lookup="tmux")
        socket = os.environ.get(_SOCKET_ENV, _DEFAULT_SOCKET)
        if not _SOCKET_NAME.fullmatch(socket):
            raise ValueError(
                f"{_SOCKET_ENV} must contain only ASCII letters, digits, '_' or '-'")
        self.socket = socket

    def _tmux_env(self) -> dict[str, str]:
        """Keep provider credentials and runtime injection out of tmux."""

        return {
            key: value
            for key, value in os.environ.items()
            if key not in _UNSAFE_TMUX_ENV_EXACT
            and not key.startswith(_UNSAFE_TMUX_ENV)
        }

    def _tmux(
        self,
        args: Sequence[str],
        *,
        capture_output: bool = True,
        clear_tmux_env: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [str(self.tmux_binary), "-L", self.socket, *args]
        environment = self._tmux_env()
        if clear_tmux_env:
            environment.pop("TMUX", None)
        return subprocess.run(
            command,
            capture_output=capture_output,
            text=True,
            check=False,
            env=environment,
        )

    @staticmethod
    def _failure(result: subprocess.CompletedProcess[str], action: str) -> RuntimeError:
        detail = (result.stderr or result.stdout or "tmux command failed").strip()
        return RuntimeError(f"{action}: {detail}")

    def _claude_binary(self) -> Path:
        configured = os.environ.get(_CLAUDE_ENV)
        return _trusted_executable(configured, label="Claude Code", lookup="claude")

    def _profile(self, profile_name: str):
        if not isinstance(profile_name, str) or not _PROFILE_NAME.fullmatch(profile_name):
            raise ValueError(f"invalid profile name: {profile_name!r}")
        return self.store.get(profile_name)

    @staticmethod
    def _inner_args(
        ccs_binary: Path,
        claude_binary: Path,
        profile_name: str,
        mode: str,
        claude_args: Sequence[str],
        config_dir: Path | None,
        no_auto: bool,
        store_home: Path,
        socket: str,
        selection_token: str | None = None,
        run_session_id: str | None = None,
        source_session_id: str | None = None,
        run_id: str | None = None,
    ) -> str:
        if mode not in {"run", "resume", "switch"}:
            raise ValueError("mode must be one of: run, resume, switch")
        if any("\x00" in str(arg) for arg in claude_args):
            raise ValueError("Claude arguments cannot contain NUL bytes")

        if mode == "run":
            if run_session_id is None:
                raise ValueError("run_session_id is required for run mode")
            run_session_id = _uuid_value(run_session_id, "run_session_id")
        elif run_session_id is not None:
            raise ValueError("run_session_id is only valid for run mode")
        if source_session_id is not None:
            source_session_id = _uuid_value(source_session_id, "source_session_id")
            if mode not in {"resume", "switch"}:
                raise ValueError("source_session_id is only valid for resume or switch mode")
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must contain 12 lowercase hexadecimal characters")

        if mode == "switch":
            command = [str(ccs_binary), "switch", "--foreground", profile_name]
        else:
            command = [
                str(ccs_binary),
                mode,
                "--foreground",
                "--profile",
                profile_name,
            ]
        if mode == "run":
            command.extend(("--managed-session-id", run_session_id))
        elif mode == "switch" and source_session_id is not None:
            command.extend(("--source-session", source_session_id))
        if no_auto:
            command.append("--no-auto")
        if selection_token is not None:
            if mode != "switch" or "\x00" in selection_token or len(selection_token) > 1024:
                raise ValueError("invalid internal selection token")
            command.extend(("--selection-token", selection_token))

        raw_args = [str(arg) for arg in claude_args]
        if mode == "resume" and source_session_id is not None:
            if raw_args and raw_args[0] != "--":
                if raw_args[0] != source_session_id:
                    raise ValueError("source_session_id conflicts with the resume session ID")
                raw_args.pop(0)
            command.append(source_session_id)
        elif mode == "resume" and raw_args and raw_args[0] != "--":
            # ``resume`` has one optional positional session ID.  Keep it
            # before the remainder marker so an explicit ID reaches ccs.
            command.append(raw_args.pop(0))
        if raw_args:
            command.extend(("--", *raw_args))

        # ``env`` makes the child account and Claude executable explicit in
        # the tmux command.  shlex.join then quotes every path and argument
        # before tmux gives the command to its shell parser.
        environment = [
            "/usr/bin/env",
            f"{_CLAUDE_ENV}={claude_binary}",
            f"CC_SWAPER_HOME={store_home}",
            f"CC_SWAPER_RUN_ID={run_id}",
            f"CC_SWAPER_TMUX_SOCKET={socket}",
        ]
        if config_dir is not None:
            environment.append(f"CLAUDE_CONFIG_DIR={config_dir}")
        return shlex.join([*environment, *command])

    @staticmethod
    def _exact_target(name: str, *, session_option: bool = False) -> str:
        # tmux's exact session target is ``=name``.  Session-scoped options
        # additionally need the empty window suffix on tmux 3.x, otherwise
        # set-option interprets the target as a window and rejects it.
        return f"={name}:" if session_option else f"={name}"

    def exists(self, cwd: Path) -> bool:
        """Return whether the project has any valid managed tmux session."""

        return bool(self.list_sessions_for_cwd(cwd))

    def _verified_session(self, cwd: Path, name: str) -> dict[str, Any]:
        canonical = _canonical_cwd(cwd)
        for item in self.list_sessions_for_cwd(canonical):
            if item["name"] == name:
                return item
        raise RuntimeError(f"tmux session {name} is missing or has invalid cc-swaper metadata")

    def _select_session(
        self,
        cwd: Path,
        session_name: str | None,
        run_id: str | None,
    ) -> dict[str, Any]:
        if session_name is not None and run_id is not None:
            raise ValueError("specify either a session name or run_id, not both")
        sessions = self.list_sessions_for_cwd(cwd)
        if session_name is not None:
            matches = [item for item in sessions if item["name"] == session_name]
        elif run_id is not None:
            if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
                raise ValueError("run_id must contain 12 lowercase hexadecimal characters")
            selected_run_id = run_id
            matches = [item for item in sessions if item["run_id"] == selected_run_id]
        else:
            matches = sessions

        if not matches:
            selected = session_name or run_id
            if selected is None:
                raise RuntimeError(f"no valid cc-swaper tmux session for {_canonical_cwd(cwd)}")
            raise RuntimeError(
                f"tmux session {selected} is missing or has invalid cc-swaper metadata"
            )
        if len(matches) > 1:
            names = ", ".join(item["name"] for item in matches)
            raise RuntimeError(
                f"multiple background sessions exist for {_canonical_cwd(cwd)}; "
                f"specify a session name or run_id: {names}"
            )
        return matches[0]

    def _cleanup_session(self, name: str) -> None:
        self._tmux(["kill-session", "-t", self._exact_target(name)])

    def start(
        self,
        cwd: Path,
        profile_name: str,
        mode: str,
        claude_args: list[str],
        detach: bool = True,
        *,
        no_auto: bool = False,
        selection_token: str | None = None,
        run_session_id: str | None = None,
        source_session_id: str | None = None,
    ) -> str:
        """Start a uniquely named project session and return its tmux name.

        Sessions are created detached first even when ``detach=False``.  That
        allows metadata and ``remain-on-exit`` to be set before an interactive
        attach, and avoids a partially configured session being visible to a
        monitor.
        """

        canonical = _canonical_cwd(cwd)
        if not canonical.is_dir():
            raise ValueError(f"project directory does not exist: {canonical}")
        profile = self._profile(profile_name)
        claude_binary = self._claude_binary()
        if mode == "run":
            if run_session_id is None:
                run_session_id = str(uuid.uuid4())
            run_session_id = _uuid_value(run_session_id, "run_session_id")
        if source_session_id is not None:
            source_session_id = _uuid_value(source_session_id, "source_session_id")
        run_id = uuid.uuid4().hex[:12]
        name = _managed_session_name(canonical, run_id)
        command = self._inner_args(
            self.ccs_binary,
            claude_binary,
            profile.name,
            mode,
            claude_args,
            profile.config_dir,
            no_auto,
            self.store.home,
            self.socket,
            selection_token,
            run_session_id,
            source_session_id,
            run_id,
        )

        created = self._tmux(
            [
                "new-session",
                "-d",
                "-s",
                name,
                "-c",
                str(canonical),
                command,
            ]
        )
        if created.returncode != 0:
            raise self._failure(created, f"could not create tmux session {name}")

        try:
            claude_session_id = run_session_id if mode == "run" else source_session_id
            tags = [
                ("remain-on-exit", "on"),
                ("@ccs_cwd", str(canonical)),
                ("@ccs_initial_profile", profile.name),
                ("@ccs_run_id", run_id),
            ]
            if claude_session_id is not None:
                tags.append(("@ccs_session_id", claude_session_id))
            for option, value in tags:
                tagged = self._tmux(
                    [
                        "set-option",
                        "-t",
                        self._exact_target(name, session_option=True),
                        option,
                        value,
                    ]
                )
                if tagged.returncode != 0:
                    raise self._failure(tagged, f"could not configure tmux session {name}")
            # A command which exits immediately is still retained as a dead
            # session by remain-on-exit.  Confirm the server kept the session
            # before handing control back to the caller.
            try:
                self._verified_session(canonical, name)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"tmux session {name} exited or has invalid metadata before it could be monitored"
                ) from exc
        except BaseException:
            self._cleanup_session(name)
            raise

        if not detach:
            if self.attach(canonical, session_name=name) != 0:
                raise RuntimeError(f"could not attach to tmux session {name}")
        return name

    def attach(
        self,
        cwd: Path,
        session_name: str | None = None,
        *,
        run_id: str | None = None,
    ) -> int:
        """Attach to one verified session, requiring a selector when ambiguous."""

        selected = self._select_session(cwd, session_name, run_id)
        name = selected["name"]
        current_socket = os.environ.get("TMUX", "").split(",", 1)[0]
        same_socket = bool(current_socket and Path(current_socket).name == self.socket)
        command = "switch-client" if same_socket else "attach-session"
        result = self._tmux(
            [command, "-t", self._exact_target(name)],
            capture_output=False,
            clear_tmux_env=bool(current_socket and not same_socket),
        )
        return result.returncode

    def stop(
        self,
        cwd: Path,
        session_name: str | None = None,
        *,
        run_id: str | None = None,
    ) -> int:
        """Stop one verified session, requiring a selector when ambiguous."""

        selected = self._select_session(cwd, session_name, run_id)
        name = selected["name"]
        result = self._tmux(["kill-session", "-t", self._exact_target(name)])
        return result.returncode

    def list_sessions(self) -> list[dict[str, Any]]:
        """List sessions and the metadata/state needed by the monitor."""

        result = self._tmux(
            [
                "list-sessions",
                "-F",
                "#{session_name}\t#{session_attached}\t#{@ccs_cwd}\t#{@ccs_initial_profile}"
                "\t#{@ccs_run_id}\t#{@ccs_session_id}",
            ]
        )
        if result.returncode != 0:
            # tmux returns 1 when its dedicated server has no sessions.
            return []

        sessions: list[dict[str, Any]] = []
        # Preserve a trailing empty session_id field for older managed or
        # legacy sessions; str.strip() would remove its tab delimiter.
        for raw_line in (result.stdout or "").rstrip("\r\n").splitlines():
            fields = raw_line.split("\t", 5)
            if len(fields) != 6:
                continue
            name, attached_raw, cwd_raw, profile, run_id_raw, session_id_raw = fields
            if not name or not cwd_raw or not profile or not _PROFILE_NAME.fullmatch(profile):
                continue
            try:
                canonical = _canonical_cwd(Path(cwd_raw))
            except (OSError, RuntimeError, ValueError):
                continue
            if not Path(cwd_raw).is_absolute() or str(canonical) != cwd_raw:
                continue
            try:
                if run_id_raw:
                    if not _RUN_ID.fullmatch(run_id_raw):
                        continue
                    if name != _managed_session_name(canonical, run_id_raw):
                        continue
                    run_id: str | None = run_id_raw
                else:
                    if name != session_name(canonical):
                        continue
                    run_id = None
                if session_id_raw:
                    session_id = _uuid_value(session_id_raw, "session_id")
                    if session_id != session_id_raw:
                        continue
                else:
                    session_id = None
            except (OSError, RuntimeError, ValueError):
                continue
            pane = self._tmux(
                [
                    "list-panes",
                    "-t",
                    self._exact_target(name),
                    "-F",
                    "#{pane_dead}\t#{pane_current_path}",
                ]
            )
            pane_dead = False
            pane_cwd = ""
            if pane.returncode == 0:
                pane_fields = _status_output(pane).splitlines()
                if pane_fields:
                    pane_values = pane_fields[0].split("\t", 1)
                    pane_dead = pane_values[0] in {"1", "true", "on"}
                    if len(pane_values) == 2:
                        pane_cwd = pane_values[1]
            sessions.append(
                {
                    "name": name,
                    "cwd": cwd_raw or pane_cwd or None,
                    "initial_profile": profile or None,
                    "profile": profile or None,
                    "run_id": run_id,
                    "session_id": session_id,
                    "attached": attached_raw not in {"", "0", "false", "off"},
                    "dead": pane_dead,
                }
            )
        return sessions

    def list_sessions_for_cwd(self, cwd: Path) -> list[dict[str, Any]]:
        """Return all valid managed sessions for a canonical project path."""

        canonical = _canonical_cwd(cwd)
        return [item for item in self.list_sessions() if item["cwd"] == str(canonical)]


__all__ = ["TmuxSessions", "session_name"]
