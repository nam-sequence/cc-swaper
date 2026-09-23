"""Command line interface for separately authenticated Claude Code profiles."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator

from . import __version__
from .profiles import Profile, ProfileStore, SelectionSnapshot
from . import shell as shell_integration
from . import service as monitor_service
from .hooks import HookMonitor, hook_settings
from .runner import (
    auth_details,
    auth_identity,
    auth_status,
    claude_binary,
    profile_environment,
    run_interactive,
    run_passthrough,
)
from .usage_table import LiveUsageTable, render_usage_table


CONTINUATION_PROMPT = (
    "Continue the current task after a Claude Code usage limit. Review the "
    "conversation and current workspace state first. Do not repeat any action "
    "whose result is already present. If a partially completed external action "
    "cannot be verified safely, ask before repeating it."
)
DISALLOWED_SESSION_FLAGS = frozenset(
    {"--session-id", "--continue", "-c", "--resume", "-r", "--fork-session", "--settings"}
)
NON_INTERACTIVE_FLAGS = frozenset(
    {"--print", "-p", "--bg", "--background", "--cloud", "--no-session-persistence"}
)
HOOK_DISABLING_FLAGS = frozenset({"--bare", "--safe-mode"})


def _error(message: str, code: int = 2) -> int:
    print(f"ccs: {message}", file=sys.stderr)
    return code


def _claude_args(args: list[str], auto: bool) -> list[str]:
    result = args[1:] if args and args[0] == "--" else args
    for arg in result:
        key = arg.split("=", 1)[0]
        if key in DISALLOWED_SESSION_FLAGS:
            raise ValueError(f"{key} conflicts with managed sessions; use 'ccs resume'")
        if key == "--no-session-persistence":
            raise ValueError("--no-session-persistence prevents conversation handoff")
        if auto and key in NON_INTERACTIVE_FLAGS:
            raise ValueError(f"{key} cannot preserve an interactive session during auto-switch")
        if auto and key in HOOK_DISABLING_FLAGS:
            raise ValueError(f"{key} disables hooks required for automatic switching")
    return result


def _session_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError("session ID must be a UUID") from exc


def _transcript(store: ProfileStore, session_id: str) -> Path | None:
    matches: list[Path] = []
    for profile in store.all():
        root = _projects_dir(profile)
        if root.is_dir():
            matches.extend(
                path for path in root.glob(f"*/{session_id}.jsonl")
                if _safe_transcript(path, root, session_id)
            )
    if not matches:
        return None
    if len(matches) > 1:
        raise RuntimeError(f"more than one transcript has session ID {session_id}")
    return matches[0]


def _projects_dir(profile: Profile) -> Path:
    root = (
        profile.config_dir / "projects"
        if profile.config_dir is not None
        else Path.home() / ".claude" / "projects"
    )
    if profile.config_dir is not None and root.is_symlink():
        raise RuntimeError(f"managed profile projects directory is a symlink: {root}")
    return root


def _transcript_owner(store: ProfileStore, transcript: Path) -> str | None:
    resolved = transcript.resolve()
    for profile in store.all():
        if resolved.is_relative_to(_projects_dir(profile).resolve()):
            return profile.name
    return None


def _safe_transcript(path: Path, root: Path, session_id: str) -> bool:
    if path.suffix != ".jsonl" or path.stem != session_id or path.is_symlink():
        return False
    try:
        info = path.stat()
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and resolved.is_relative_to(root.resolve())
    )


def _reported_transcript(profile: Profile, session_id: str | None, path: Path | None) -> Path | None:
    if session_id is None or path is None:
        return None
    if not _safe_transcript(path, _projects_dir(profile), session_id):
        return None
    return path


@contextmanager
def _conversation_lock(store: ProfileStore, session_id: str) -> Iterator[None]:
    lock_dir = store.home / "locks"
    if lock_dir.is_symlink():
        raise RuntimeError(f"unsafe lock directory: {lock_dir}")
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    lock_info = lock_dir.stat()
    if lock_info.st_uid != os.getuid() or stat.S_IMODE(lock_info.st_mode) & 0o077:
        raise RuntimeError(f"lock directory is not private: {lock_dir}")
    digest = hashlib.sha256(_session_id(session_id).encode()).hexdigest()[:24]
    path = lock_dir / f"conversation-{digest}.lock"
    fd = os.open(
        path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        lock_file_info = os.fstat(fd)
        if not stat.S_ISREG(lock_file_info.st_mode) or lock_file_info.st_uid != os.getuid():
            raise RuntimeError(f"unsafe lock file: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "this Claude conversation is already running; use 'ccs attach'"
            ) from exc
        yield
    finally:
        os.close(fd)


@contextmanager
def _profile_lock(store: ProfileStore, name: str, *, exclusive: bool) -> Iterator[None]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("invalid profile name")
    lock_dir = store.home / "locks"
    if lock_dir.is_symlink():
        raise RuntimeError(f"unsafe lock directory: {lock_dir}")
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    dir_info = lock_dir.stat()
    if dir_info.st_uid != os.getuid() or stat.S_IMODE(dir_info.st_mode) & 0o077:
        raise RuntimeError(f"profile lock directory is not private: {lock_dir}")
    path = lock_dir / f"profile-{name}.lock"
    fd = os.open(
        path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError(f"unsafe profile lock: {path}")
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"profile '{name}' is currently in use") from exc
        yield
    finally:
        os.close(fd)


def _select_profile(store: ProfileStore, name: str | None) -> Profile:
    return store.get(name) if name else store.selected()


def _choose_profile_name(store: ProfileStore, command: str) -> str | None:
    """Choose a profile without changing selection until the caller acts."""

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError(
            f"'{command}' needs an interactive terminal to choose an account; "
            f"use 'ccs {command} <profile>'"
        )
    profiles = store.all()
    if not profiles:
        raise RuntimeError("no account profiles are configured; run 'ccs init'")
    selected = store.selected().name
    print("Choose an account:")
    for index, profile in enumerate(profiles, start=1):
        marker = " (selected)" if profile.name == selected else ""
        display_name = f"@{profile.name}" if profile.name.isdecimal() else profile.name
        print(f"  {index}. {display_name}{marker}")
    print("  0. Cancel")
    while True:
        try:
            choice = input("Enter a number or account name: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not choice or choice.casefold() in {"0", "q", "quit"}:
            return None
        if choice.startswith("@"):
            for profile in profiles:
                if choice[1:] == profile.name:
                    return profile.name
        if choice.isdecimal():
            index = int(choice)
            if 1 <= index <= len(profiles):
                return profiles[index - 1].name
        else:
            for profile in profiles:
                if choice == profile.name:
                    return profile.name
        print(f"Invalid choice. Enter a number from 1 to {len(profiles)}, or 0 to cancel.")


def _choose_managed_session(sessions: list[dict[str, object]], action: str) -> str | None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError(
            f"multiple sessions exist in this project; use 'ccs {action} --session <run-id>'"
        )
    print(f"Choose a session to {action}:")
    for index, item in enumerate(sessions, start=1):
        state = "exited" if item.get("dead") else (
            "attached" if item.get("attached") else "detached"
        )
        run_id = item.get("run_id") or "legacy"
        profile = item.get("initial_profile") or "?"
        print(f"  {index}. {run_id}  {profile}  {state}  {item['name']}")
    print("  0. Cancel")
    while True:
        try:
            choice = input("Enter a session number: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not choice or choice == "0":
            return None
        if choice.isdecimal() and 1 <= int(choice) <= len(sessions):
            return str(sessions[int(choice) - 1]["name"])
        print(f"Invalid choice. Enter a number from 1 to {len(sessions)}, or 0 to cancel.")


def _encode_selection_snapshot(snapshot: SelectionSnapshot) -> str:
    return json.dumps(
        {"name": snapshot.name, "revision": snapshot.revision}, separators=(",", ":")
    )


def _decode_selection_snapshot(raw: str) -> SelectionSnapshot:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("invalid internal selection token") from None
    if not isinstance(value, dict) or set(value) != {"name", "revision"}:
        raise ValueError("invalid internal selection token")
    name = value["name"]
    revision = value["revision"]
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise ValueError("invalid internal selection token")
    return SelectionSnapshot(name, revision)


def _require_login(profile: Profile, binary: str) -> None:
    logged_in, method = auth_status(profile, binary)
    if not logged_in:
        raise RuntimeError(
            f"profile '{profile.name}' is not signed in with claude.ai "
            f"(authentication: {method}); run 'ccs login {profile.name}'"
        )


def _next_profile(
    store: ProfileStore, current: Profile, attempted: set[str], binary: str
) -> Profile | None:
    profiles = store.all()
    current_identity = auth_identity(current, binary)
    position = next(index for index, profile in enumerate(profiles) if profile.name == current.name)
    for offset in range(1, len(profiles)):
        candidate = profiles[(position + offset) % len(profiles)]
        if candidate.name in attempted:
            continue
        logged_in, _ = auth_status(candidate, binary)
        if logged_in:
            candidate_identity = auth_identity(candidate, binary)
            if current_identity is not None and candidate_identity == current_identity:
                print(
                    f"ccs: skipping '{candidate.name}' (same Claude account as '{current.name}')",
                    file=sys.stderr,
                )
                continue
            return candidate
        print(f"ccs: skipping '{candidate.name}' (run 'ccs login {candidate.name}')", file=sys.stderr)
    return None


def _background_session(
    store: ProfileStore,
    *,
    mode: str,
    profile_name: str,
    claude_args: list[str],
    no_auto: bool,
    selection_snapshot: SelectionSnapshot | None = None,
    source_session_id: str | None = None,
    attach: bool = False,
) -> int:
    from .tmux_sessions import TmuxSessions

    profile = store.get(profile_name)
    cwd = Path.cwd().resolve()
    manager = TmuxSessions(store)
    if mode in {"resume", "switch"}:
        explicitly_chosen_source = source_session_id is not None or (
            mode == "resume" and bool(claude_args)
        )
        active = [item for item in manager.list_sessions_for_cwd(cwd) if not item.get("dead")]
        unknown_legacy = [
            item for item in active
            if item.get("run_id") is None and item.get("session_id") is None
        ]
        explicit = source_session_id or (claude_args[0] if mode == "resume" and claude_args else None)
        if explicit is None and store.last_session(cwd) is None and unknown_legacy:
            raise RuntimeError(
                "a legacy background session is active but its conversation ID is unknown; "
                f"use 'ccs attach --session {unknown_legacy[0]['name']}'"
            )
        session_id = _session_id(explicit or store.last_session(cwd) or "")
        if _transcript(store, session_id) is None:
            raise RuntimeError(f"transcript for {session_id} was not found")
        for item in active:
            run_id = item.get("run_id")
            record = store.background_session(run_id) if isinstance(run_id, str) else None
            active_id = record.get("id") if record else item.get("session_id")
            if active_id is None and run_id is None:
                continue
            if active_id == session_id:
                if mode == "resume" and attach:
                    return manager.attach(cwd, session_name=str(item["name"]))
                raise RuntimeError(
                    f"conversation {session_id} is already active; "
                    f"use 'ccs attach --session {item['name']}'"
                )
        if unknown_legacy:
            raise RuntimeError(
                "a legacy background session is active and its conversation ID cannot be verified; "
                f"use 'ccs attach --session {unknown_legacy[0]['name']}' or stop it before resuming"
            )
    _require_login(profile, claude_binary())
    extra = (
        {"selection_token": _encode_selection_snapshot(selection_snapshot)}
        if selection_snapshot is not None else {}
    )
    if mode in {"resume", "switch"}:
        extra["source_session_id"] = session_id
    name = manager.start(
        cwd, profile.name, mode, claude_args, detach=not attach, no_auto=no_auto, **extra
    )
    if attach:
        print(f"Detached from background session {name}; it is still running.")
    else:
        print(f"Started background session {name}.")
        print(f"Use 'ccs attach --session {name}' to interact; detach with Ctrl-b d.")
    return 0


def _run_session(
    store: ProfileStore,
    *,
    resume: bool,
    explicit_session: str | None,
    profile_name: str | None,
    no_auto: bool,
    passthrough: list[str],
    continue_now: bool = False,
    selection_snapshot: SelectionSnapshot | None = None,
    managed_session_id: str | None = None,
) -> int:
    chosen = _select_profile(store, profile_name)
    with ExitStack() as lifetime_locks:
        lifetime_locks.enter_context(_profile_lock(store, chosen.name, exclusive=False))
        # A removal can win before the lock. Reload after acquisition so an
        # archived or replaced profile is never launched from a stale object.
        store.get_fresh(chosen.name)
        return _run_session_impl(
            store, resume=resume, explicit_session=explicit_session,
            profile_name=chosen.name, no_auto=no_auto, passthrough=passthrough,
            continue_now=continue_now, selection_snapshot=selection_snapshot,
            managed_session_id=managed_session_id,
            lifetime_locks=lifetime_locks, locked_profiles={chosen.name},
        )


def _run_session_impl(
    store: ProfileStore,
    *,
    resume: bool,
    explicit_session: str | None,
    profile_name: str | None,
    no_auto: bool,
    passthrough: list[str],
    continue_now: bool = False,
    selection_snapshot: SelectionSnapshot | None = None,
    managed_session_id: str | None = None,
    lifetime_locks: ExitStack,
    locked_profiles: set[str],
) -> int:
    binary = claude_binary()
    profile = _select_profile(store, profile_name)
    _require_login(profile, binary)
    claude_args = _claude_args(passthrough, auto=not no_auto)
    if os.environ.get("CLAUDE_CODE_SKIP_PROMPT_HISTORY") not in (None, "", "0", "false"):
        raise RuntimeError("CLAUDE_CODE_SKIP_PROMPT_HISTORY prevents conversation handoff")
    if not no_auto and any(
        os.environ.get(key) not in (None, "", "0", "false")
        for key in ("CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SAFE_MODE")
    ):
        raise RuntimeError("Claude Code simple/safe mode disables hooks required for auto-switch")
    cwd = Path.cwd().resolve()
    run_id = os.environ.get("CC_SWAPER_RUN_ID")
    if run_id is not None and not re.fullmatch(r"[0-9a-f]{12}", run_id):
        raise ValueError("invalid managed run ID")
    if run_id is not None:
        tmux_socket = os.environ.get("TMUX", "").split(",", 1)[0]
        expected_socket = os.environ.get("CC_SWAPER_TMUX_SOCKET", "cc-swaper")
        if Path(tmux_socket).name != expected_socket:
            raise RuntimeError("managed run ID is only valid inside its cc-swaper tmux socket")
    if resume:
        if managed_session_id is not None:
            raise ValueError("managed session ID cannot be used with resume")
        session_id = _session_id(explicit_session or store.last_session(cwd) or "")
        recorded = store.last_transcript(cwd) if explicit_session is None else None
        if recorded is not None:
            owner_name, recorded_path = recorded
            if not _safe_transcript(
                recorded_path, _projects_dir(store.get(owner_name)), session_id
            ):
                raise RuntimeError("recorded session transcript is missing or unsafe")
            transcript = recorded_path
        else:
            transcript = _transcript(store, session_id)
        if transcript is None:
            raise RuntimeError(
                f"transcript for {session_id} was not found; refusing to start a new conversation"
            )
        forked = _transcript_owner(store, transcript) != profile.name
        initial_args = [*claude_args, "--resume", str(transcript)]
        if forked:
            initial_args.extend(["--fork-session", "--permission-mode", "manual"])
        if continue_now:
            initial_args.append(CONTINUATION_PROMPT)
    else:
        session_id = _session_id(managed_session_id) if managed_session_id else str(uuid.uuid4())
        initial_args = [*claude_args, "--session-id", session_id]
        forked = False

    source_owner = _transcript_owner(store, transcript) if resume else None
    if source_owner is not None and source_owner not in locked_profiles:
        lifetime_locks.enter_context(_profile_lock(store, source_owner, exclusive=False))
        locked_profiles.add(source_owner)
        store.get_fresh(source_owner)
    locked_conversations = {session_id}

    def hold_conversation(new_id: str) -> None:
        if new_id not in locked_conversations:
            lifetime_locks.enter_context(_conversation_lock(store, new_id))
            locked_conversations.add(new_id)

    with _conversation_lock(store, session_id):
        if resume:
            owner_name = _transcript_owner(store, transcript)
            if owner_name is None or not _safe_transcript(
                transcript, _projects_dir(store.get(owner_name)), session_id
            ):
                raise RuntimeError("session transcript changed before Claude could resume it")
            store.set_last_session(
                cwd, session_id, profile_name=owner_name, transcript_path=transcript
            )
        else:
            store.set_last_session(cwd, session_id)
        if run_id is not None:
            store.set_background_session(
                run_id, cwd, session_id, profile.name, transcript if resume else None,
                transcript_profile_name=source_owner if resume else profile.name,
            )
        needs_initial_select = selection_snapshot is not None

        def mark_selected() -> None:
            nonlocal needs_initial_select
            if needs_initial_select:
                assert selection_snapshot is not None
                store.select_if_unchanged(selection_snapshot, profile.name)
                needs_initial_select = False

        attempted = {profile.name}
        pending_selection: str | None = None
        while True:
            print(f"\r\nccs: Claude Code profile '{profile.name}'\r\n", file=sys.stderr)
            with tempfile.TemporaryDirectory(prefix="run-", dir=store.home) as run_dir:
                event_file = Path(run_dir) / "events.jsonl"
                event_file.touch(mode=0o600)
                with _profile_lock(store, profile.name, exclusive=False):
                    profile = store.get_fresh(profile.name)
                    _require_login(profile, binary)
                    if pending_selection == profile.name:
                        store.select(profile.name)
                        pending_selection = None
                    with HookMonitor(
                        event_file,
                        profile_projects=_projects_dir(profile),
                        expected_session_id=None if forked else session_id,
                    ) as monitor:
                        recorded_started_id: str | None = None

                        def on_started() -> None:
                            nonlocal recorded_started_id
                            if monitor.session_id is not None:
                                hold_conversation(monitor.session_id)
                            mark_selected()
                            if (
                                run_id is not None
                                and monitor.session_id is not None
                                and monitor.session_id != recorded_started_id
                            ):
                                store.set_background_session(
                                    run_id, cwd, monitor.session_id, profile.name,
                                    monitor.transcript_path,
                                )
                                recorded_started_id = monitor.session_id

                        launch_args = ["--settings", hook_settings(event_file), *initial_args]
                        if no_auto:
                            environment = profile_environment(profile)
                            code = run_passthrough(
                                binary, launch_args, environment, monitor=monitor,
                                on_session_started=on_started,
                            )
                            monitor.poll()
                            if monitor.session_id is not None or code == 0:
                                on_started()
                            if monitor.session_id and _reported_transcript(
                                profile, monitor.session_id, monitor.transcript_path
                            ):
                                store.set_last_session(
                                    cwd, monitor.session_id, profile_name=profile.name,
                                    transcript_path=monitor.transcript_path,
                                )
                                if run_id is not None:
                                    store.set_background_session(
                                        run_id, cwd, monitor.session_id, profile.name,
                                        monitor.transcript_path,
                                    )
                            return code
                        result = run_interactive(
                            binary,
                            launch_args,
                            profile_environment(profile),
                            monitor=monitor,
                            on_session_started=on_started,
                        )
            if result.session_id:
                hold_conversation(result.session_id)
                reported = _reported_transcript(
                    profile, result.session_id, result.transcript_path
                )
                if reported:
                    session_id = result.session_id
                    store.set_last_session(
                        cwd, session_id, profile_name=profile.name, transcript_path=reported
                    )
                    if run_id is not None:
                        store.set_background_session(
                            run_id, cwd, session_id, profile.name, reported
                        )
            if not result.exhausted:
                return result.returncode
            if forked and result.session_id is None:
                return _error(
                    "usage limit detected after a fork, but Claude did not report the new session ID; "
                    "stopped to avoid resuming an older branch",
                    75,
                )
            transcript = _reported_transcript(
                profile, result.session_id, result.transcript_path
            ) or _transcript(store, session_id)
            if transcript is None:
                return _error(
                    "usage limit detected, but the session transcript was not saved; "
                    "stopped to avoid losing context",
                    75,
                )
            following = _next_profile(store, profile, attempted, binary)
            if following is None:
                return _error(
                    f"all signed-in profiles have been tried; resume session {session_id} after a limit resets",
                    75,
                )
            if following.name not in locked_profiles:
                lifetime_locks.enter_context(_profile_lock(store, following.name, exclusive=False))
                locked_profiles.add(following.name)
            following = store.get_fresh(following.name)
            _require_login(following, binary)
            profile = following
            pending_selection = following.name
            attempted.add(profile.name)
            print(
                f"\r\nccs: usage limit detected; resuming session {session_id} with '{profile.name}'\r\n",
                file=sys.stderr,
            )
            # The original request is not replayed. Claude sees the transcript
            # and a cautious continuation message in the same conversation.
            initial_args = [
                "--permission-mode", "manual", "--resume", str(transcript),
                "--fork-session", CONTINUATION_PROMPT,
            ]
            forked = True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccs", description="Switch Claude Code accounts while keeping a resumable conversation"
    )
    parser.add_argument("--version", action="version", version=f"ccs {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="register the current default Claude Code login")
    init.add_argument("--name", default="main", help="profile name (default: main)")
    add = commands.add_parser("add", help="create an isolated profile and sign in")
    add.add_argument("name")
    add.add_argument("--email", help="prefill the Claude login page")
    add.add_argument("--no-login", action="store_true", help="create profile without opening login")
    login = commands.add_parser("login", help="sign in through Claude Code's official flow")
    login.add_argument("name")
    login.add_argument("--email")
    list_command = commands.add_parser("list", help="list profiles and login state")
    list_command.add_argument("--show-identity", action="store_true", help="show login email and org")
    use = commands.add_parser("use", help="select a profile for the next session")
    use.add_argument("name", nargs="?", help="profile name (omit to choose from a list)")
    run = commands.add_parser("run", help="start an interactive Claude Code session")
    run.add_argument("--profile")
    run.add_argument("--foreground", action="store_true", help="run directly in this terminal")
    run_output = run.add_mutually_exclusive_group()
    run_output.add_argument("--attach", action="store_true", help="attach to the new background session")
    run_output.add_argument("--detach", action="store_true", help="return to the shell after starting")
    run.add_argument("--no-auto", action="store_true", help="do not switch on usage limit")
    run.add_argument("--managed-session-id", help=argparse.SUPPRESS)
    run.add_argument("claude_args", nargs=argparse.REMAINDER)
    resume = commands.add_parser("resume", help="resume the last session in this directory")
    resume.add_argument("session", nargs="?", help="optional session UUID")
    resume.add_argument("--profile")
    resume.add_argument("--foreground", action="store_true", help="run directly in this terminal")
    resume_output = resume.add_mutually_exclusive_group()
    resume_output.add_argument("--attach", action="store_true", help="attach to the resumed background session")
    resume_output.add_argument("--detach", action="store_true", help="return to the shell after starting")
    resume.add_argument("--no-auto", action="store_true")
    switch = commands.add_parser("switch", help="select another profile and continue the last session")
    switch.add_argument("name", nargs="?", help="profile name (omit to choose from a list)")
    switch.add_argument("--foreground", action="store_true", help="run directly in this terminal")
    switch.add_argument("--no-auto", action="store_true")
    switch.add_argument("--selection-token", help=argparse.SUPPRESS)
    switch.add_argument("--source-session", help=argparse.SUPPRESS)
    attach = commands.add_parser("attach", help="attach to this project's background session")
    attach.add_argument("--project", type=Path)
    attach.add_argument("--session", help="managed run ID or full tmux session name")
    stop = commands.add_parser("stop", help="stop this project's background session")
    stop.add_argument("--project", type=Path)
    stop.add_argument("--session", help="managed run ID or full tmux session name")
    native = commands.add_parser("native", help="run a native Claude command with the selected profile")
    native.add_argument("claude_args", nargs=argparse.REMAINDER)
    shell_command = commands.add_parser("shell", help="manage the interactive Zsh claude wrapper")
    shell_command.add_argument("action", choices=("install", "uninstall", "status"))
    service_command = commands.add_parser("service", help="manage the always-on session monitor")
    service_command.add_argument("action", choices=("install", "uninstall", "status", "run"))
    commands.add_parser("setup", help="install Zsh integration and start the background monitor")
    commands.add_parser("status", help="show monitor health and managed sessions")
    usage = commands.add_parser("usage", help="show five-hour and weekly subscription usage")
    usage.add_argument("profiles", nargs="*", help="profile names (default: all)")
    usage.add_argument("--json", action="store_true", help="print machine-readable JSON")
    usage.add_argument("--timeout", type=float, default=45, help="seconds to wait per profile")
    remove = commands.add_parser("remove", help="remove a managed account profile")
    remove.add_argument("name")
    remove.add_argument(
        "--purge-data", action="store_true",
        help="also permanently delete this profile's local Claude settings and transcripts",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        store = ProfileStore()
        if args.command == "init":
            profile = store.add_default(args.name)
            print(f"Registered default Claude Code login as '{profile.name}'.")
            return 0
        if args.command == "add":
            with _profile_lock(store, args.name, exclusive=True):
                profile = store.add_managed(args.name)
                print(f"Created profile '{profile.name}'.")
                if args.no_login:
                    print(f"Run 'ccs login {profile.name}' to sign in.")
                    return 0
                login_args = [claude_binary(), "auth", "login", "--claudeai"]
                if args.email:
                    login_args.extend(["--email", args.email])
                return subprocess.call(login_args, env=profile_environment(profile))
        if args.command == "login":
            profile = store.get(args.name)
            login_args = [claude_binary(), "auth", "login", "--claudeai"]
            if args.email:
                login_args.extend(["--email", args.email])
            with _profile_lock(store, profile.name, exclusive=True):
                return subprocess.call(login_args, env=profile_environment(profile))
        if args.command == "list":
            binary = claude_binary()
            selected = store.selected().name
            for profile in store.all():
                logged_in, method = auth_status(profile, binary)
                marker = "*" if profile.name == selected else " "
                state = "signed in" if logged_in else f"not ready ({method})"
                if args.show_identity and logged_in:
                    details = auth_details(profile, binary) or {}
                    email = str(details.get("email") or "unknown email")
                    org = str(details.get("orgName") or "")
                    state += f"  {email}" + (f"  [{org}]" if org else "")
                print(f"{marker} {profile.name:16} {state}")
            return 0
        if args.command == "use":
            name = args.name or _choose_profile_name(store, "use")
            if name is None:
                print("Account selection canceled.")
                return 0
            store.select(name)
            print(f"Selected '{name}'. Run 'ccs resume' to continue the last session here.")
            return 0
        if args.command == "native":
            profile = store.selected()
            native_args = args.claude_args[1:] if args.claude_args and args.claude_args[0] == "--" else args.claude_args
            with _profile_lock(store, profile.name, exclusive=False):
                return run_passthrough(claude_binary(), native_args, profile_environment(profile))
        if args.command == "shell":
            if args.action == "install":
                path = shell_integration.install(store)
                print(f"Installed Zsh integration in {path}. Open a new terminal or run: source {path}")
            elif args.action == "uninstall":
                path = shell_integration.uninstall(store)
                print(f"Removed Zsh integration from {path}. Open a new terminal to apply it.")
            else:
                print("installed" if shell_integration.installed(store) else "not installed")
            return 0
        if args.command == "service":
            if args.action == "install":
                path = monitor_service.install(store)
                print(f"Started background monitor via {path}.")
            elif args.action == "uninstall":
                path = monitor_service.uninstall(store)
                print(f"Stopped background monitor and removed {path}.")
            elif args.action == "run":
                return monitor_service.run(store)
            else:
                state = monitor_service.status(store)
                print("running" if state["healthy"] else "not healthy")
            return 0
        if args.command == "setup":
            shell_path = shell_integration.install(store)
            service_path = monitor_service.install(store)
            print(f"Zsh integration: {shell_path}")
            print(f"Background monitor: {service_path}")
            return 0
        if args.command == "status":
            state = monitor_service.status(store)
            label = "running" if state["healthy"] else ("loaded but stale" if state["loaded"] else "not installed")
            print(f"monitor: {label}")
            for session in state["sessions"]:
                if isinstance(session, dict):
                    attachment = "exited" if session.get("dead") else ("attached" if session.get("attached") else "detached")
                    fields = (
                        session.get("run_id") or "legacy", session.get("name") or "?", attachment,
                        session.get("profile") or "?", session.get("cwd") or "?",
                        session.get("session_id") or "?",
                    )
                    print("  ".join(json.dumps(str(value), ensure_ascii=True) for value in fields))
            if state.get("error"):
                print(f"monitor error: {json.dumps(str(state['error']), ensure_ascii=True)}", file=sys.stderr)
            return 0
        if args.command == "usage":
            from .usage import UsageError, fetch_usage

            binary = claude_binary()
            names = list(dict.fromkeys(args.profiles)) if args.profiles else [p.name for p in store.all()]
            profiles = [store.get(name) for name in names]
            if not profiles:
                raise RuntimeError("no account profiles are configured; run 'ccs init'")
            checked_at = datetime.now(timezone.utc).isoformat()
            reports_by_name: dict[str, dict[str, object]] = {}
            cancelled = threading.Event()

            def collect(profile: Profile) -> dict[str, object]:
                try:
                    with _profile_lock(store, profile.name, exclusive=False):
                        snapshot = fetch_usage(
                            profile, binary, timeout=args.timeout, cancel_event=cancelled
                        )
                    return {
                        "profile": profile.name,
                        "plan": snapshot.plan,
                        "five_hour": {
                            "used_percent": snapshot.session_percent,
                            "resets_at": snapshot.session_reset_text,
                        },
                        "seven_day": {
                            "used_percent": snapshot.weekly_percent,
                            "resets_at": snapshot.weekly_reset_text,
                        },
                        "model_weekly": [
                            {"model": label, "used_percent": percent, "resets_at": reset}
                            for label, percent, reset in snapshot.model_weekly
                        ],
                    }
                except (UsageError, RuntimeError, OSError, ValueError) as exc:
                    return {"profile": profile.name, "error": str(exc)}

            live_enabled = (
                not args.json and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
            )
            columns = min(120, shutil.get_terminal_size((100, 30)).columns)
            started = time.monotonic()
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            frame = 0
            pending_futures = {}
            try:
                with ThreadPoolExecutor(max_workers=min(8, len(profiles))) as executor:
                    try:
                        for profile in profiles:
                            pending_futures[executor.submit(collect, profile)] = profile.name
                        with LiveUsageTable(sys.stdout, enabled=live_enabled) as live:
                            while pending_futures:
                                if live_enabled:
                                    elapsed = time.monotonic() - started
                                    pending = {
                                        name: (
                                            f"{frames[frame % len(frames)]} Loading {elapsed:.1f}s"
                                            if future.running() else "Queued"
                                        )
                                        for future, name in pending_futures.items()
                                    }
                                    live.draw(render_usage_table(
                                        names, reports_by_name, pending,
                                        columns=columns, compact=True,
                                    ))
                                    frame += 1
                                done, _ = wait(
                                    pending_futures, timeout=0.15, return_when=FIRST_COMPLETED
                                )
                                for future in done:
                                    name = pending_futures.pop(future)
                                    try:
                                        reports_by_name[name] = future.result()
                                    except Exception:
                                        reports_by_name[name] = {
                                            "profile": name, "error": "could not read Claude usage"
                                        }
                    except KeyboardInterrupt:
                        cancelled.set()
                        for future in pending_futures:
                            future.cancel()
                        raise
            except KeyboardInterrupt:
                print("Usage check canceled.", file=sys.stderr)
                return 130

            reports = [reports_by_name[name] for name in names]
            failed = any("error" in report for report in reports)
            if args.json:
                print(json.dumps({
                    "source": "Claude Code /usage",
                    "checked_at": checked_at,
                    "accounts": reports,
                }, ensure_ascii=False, indent=2))
            else:
                print(render_usage_table(names, reports_by_name, {}, columns=columns))
            return 1 if failed else 0
        if args.command in {"attach", "stop"}:
            from .tmux_sessions import TmuxSessions

            cwd = (args.project or Path.cwd()).resolve()
            manager = TmuxSessions(store)
            candidates = manager.list_sessions_for_cwd(cwd)
            if args.command == "attach":
                candidates = [item for item in candidates if not item.get("dead")]
            if not candidates:
                raise RuntimeError(f"no {'active ' if args.command == 'attach' else ''}background session for {cwd}")
            if args.session:
                selected = next(
                    (
                        item for item in candidates
                        if item.get("run_id") == args.session or item.get("name") == args.session
                    ),
                    None,
                )
                if selected is None:
                    raise RuntimeError(f"managed session '{args.session}' was not found in {cwd}")
                target = str(selected["name"])
            elif len(candidates) == 1:
                selected = candidates[0]
                target = str(selected["name"])
            else:
                target = _choose_managed_session(candidates, args.command)
                if target is None:
                    print("Session selection canceled.")
                    return 0
                selected = next(item for item in candidates if item["name"] == target)
            result = (
                manager.attach(cwd, session_name=target)
                if args.command == "attach"
                else manager.stop(cwd, session_name=target)
            )
            if result == 0 and args.command == "stop" and isinstance(selected.get("run_id"), str):
                store.remove_background_session(str(selected["run_id"]))
            return result
        if args.command == "remove":
            profile = store.get(args.name)
            if profile.config_dir is None:
                raise ValueError("the default Claude profile cannot be removed")
            with _profile_lock(store, profile.name, exclusive=True):
                archive = store.home / "removed"
                if archive.is_symlink():
                    raise RuntimeError(f"unsafe archive directory: {archive}")
                archive.mkdir(mode=0o700, exist_ok=True)
                archive_info = archive.stat()
                if archive_info.st_uid != os.getuid() or stat.S_IMODE(archive_info.st_mode) & 0o077:
                    raise RuntimeError(f"archive directory is not private: {archive}")
                logged_in, method = auth_status(profile, claude_binary())
                if method == "status unavailable":
                    raise RuntimeError("cannot verify Claude login status; profile was kept")
                if logged_in or method != "none":
                    code = run_passthrough(
                        claude_binary(), ["auth", "logout"], profile_environment(profile)
                    )
                    if code != 0:
                        raise RuntimeError("Claude logout failed; profile was kept")
                    still_logged_in, after_method = auth_status(profile, claude_binary())
                    if still_logged_in or after_method == "status unavailable":
                        raise RuntimeError("could not verify Claude logout; profile was kept")
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                destination = archive / f"{profile.name}-{stamp}-{uuid.uuid4().hex[:8]}"
                destination = store.remove_managed(
                    profile.name, archive_to=destination, purge_data=args.purge_data
                )
                if args.purge_data:
                    shutil.rmtree(destination)
                    store.finish_purge(destination)
                    print(f"Removed '{profile.name}' and deleted its local data.")
                else:
                    print(f"Removed '{profile.name}'. Local history was archived at {destination}.")
            return 0
        if args.command == "switch":
            name = args.name or _choose_profile_name(store, "switch")
            if name is None:
                print("Account switch canceled.")
                return 0
            if args.selection_token is not None and not args.foreground:
                raise ValueError("internal selection token requires --foreground")
            selection_snapshot = (
                _decode_selection_snapshot(args.selection_token)
                if args.selection_token is not None else store.selection_snapshot()
            )
            if not args.foreground:
                return _background_session(
                    store, mode="switch", profile_name=name,
                    claude_args=[], no_auto=args.no_auto,
                    selection_snapshot=selection_snapshot,
                    source_session_id=args.source_session,
                )
            return _run_session(
                store, resume=True, explicit_session=args.source_session, profile_name=name,
                no_auto=args.no_auto, passthrough=[], continue_now=True,
                selection_snapshot=selection_snapshot,
            )
        if args.command == "run":
            if args.managed_session_id is not None and not args.foreground:
                raise ValueError("managed session ID requires --foreground")
            if args.foreground and (args.attach or args.detach):
                raise ValueError("--foreground cannot be combined with --attach or --detach")
            if args.attach and not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise ValueError("--attach requires an interactive terminal; use --detach instead")
            forwarded = _claude_args(args.claude_args, auto=not args.no_auto)
            direct_only = any(arg in {"-p", "--print", "--bg", "--background"} for arg in forwarded)
            if direct_only and args.attach:
                raise ValueError("--attach requires an interactive Claude session")
            if not args.foreground and not direct_only:
                attach = args.attach or (
                    not args.detach and sys.stdin.isatty() and sys.stdout.isatty()
                )
                chosen = args.profile or store.selected().name
                return _background_session(
                    store, mode="run", profile_name=chosen,
                    claude_args=forwarded, no_auto=args.no_auto, attach=attach,
                )
            return _run_session(
                store, resume=False, explicit_session=None, profile_name=args.profile,
                no_auto=args.no_auto, passthrough=args.claude_args,
                managed_session_id=args.managed_session_id,
            )
        if args.command == "resume":
            if args.foreground and (args.attach or args.detach):
                raise ValueError("--foreground cannot be combined with --attach or --detach")
            if args.attach and not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise ValueError("--attach requires an interactive terminal; use --detach instead")
            if not args.foreground:
                attach = args.attach or (
                    not args.detach and sys.stdin.isatty() and sys.stdout.isatty()
                )
                chosen = args.profile or store.selected().name
                return _background_session(
                    store, mode="resume", profile_name=chosen,
                    claude_args=[args.session] if args.session else [], no_auto=args.no_auto,
                    attach=attach,
                )
            return _run_session(
                store, resume=True, explicit_session=args.session, profile_name=args.profile,
                no_auto=args.no_auto, passthrough=[],
            )
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        return _error(str(exc))
    return _error("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
