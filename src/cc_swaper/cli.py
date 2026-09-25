"""Small, manual Claude Code profile selector and usage viewer."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import select
import shutil
import stat
import subprocess
import sys
import termios
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import __version__
from . import shell as shell_integration
from .profiles import Profile, ProfileStore
from .runner import auth_details, auth_status, claude_binary, profile_environment, run_passthrough
from .usage_table import LiveUsageTable, render_usage_table


def _error(message: str, code: int = 2) -> int:
    print(f"ccs: {message}", file=sys.stderr)
    return code


def _identity_text(value: object, fallback: str = "") -> str:
    text = str(value or fallback)
    return "".join(character if character.isprintable() else "?" for character in text)


@contextmanager
def _profile_lock(store: ProfileStore, name: str, *, exclusive: bool) -> Iterator[None]:
    """Keep a profile in place while Claude or a usage check is using it."""

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("invalid profile name")
    lock_dir = store.home / "locks"
    if lock_dir.is_symlink():
        raise RuntimeError(f"unsafe lock directory: {lock_dir}")
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    info = lock_dir.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
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


def _choose_profile_name(store: ProfileStore) -> str | None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError(
            "'switch' needs an interactive terminal to choose an account; "
            "use 'ccs switch <profile>'"
        )
    profiles = store.all()
    if not profiles:
        raise RuntimeError("no account profiles are configured; run 'ccs init'")
    selected = store.selected().name
    try:
        columns = os.get_terminal_size(sys.stdout.fileno()).columns
    except OSError:
        columns = 80
    if columns < 1:
        columns = 80
    option_width = max(0, columns - 1)

    def render(cursor: int) -> None:
        sys.stdout.write("\x1b[u")
        for index, profile in enumerate(profiles):
            name = f"@{profile.name}" if profile.name.isdecimal() else profile.name
            marker = " (selected)" if profile.name == selected else ""
            pointer = "> " if index == cursor else "  "
            row = (pointer + name + marker)[:option_width]
            sys.stdout.write("\r\x1b[2K" + row)
            if index + 1 < len(profiles):
                sys.stdout.write("\n")
        sys.stdout.flush()

    def read_key(fd: int) -> str:
        """Read one menu key, bounding waits for partial ANSI sequences."""

        def read_byte(timeout: float | None) -> bytes | None:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return None
            return os.read(fd, 1)

        first = read_byte(None)
        if first in (b"\r", b"\n"):
            return "enter"
        if first in (b"\x03", b"\x1a"):
            return "cancel"
        if first in (b"", None):
            return "cancel"
        if first != b"\x1b":
            return "ignore"

        # A lone Esc cancels promptly. CSI and SS3 arrow sequences may arrive
        # byte by byte, so only wait a short, bounded time for their remainder.
        prefix = read_byte(0.08)
        if prefix is None or prefix == b"":
            return "cancel"
        if prefix not in (b"[", b"O"):
            return "ignore"

        deadline = time.monotonic() + 0.12
        sequence_length = 0
        while sequence_length < 32:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "ignore"
            byte = read_byte(remaining)
            if byte is None or byte == b"":
                return "ignore"
            sequence_length += 1
            value = byte[0]
            # The final byte of a CSI/SS3 sequence is in this range. This also
            # consumes modified arrows such as ESC [ 1 ; 5 A safely.
            if 0x40 <= value <= 0x7E:
                if value == ord("A"):
                    return "up"
                if value == ord("B"):
                    return "down"
                return "ignore"
        return "ignore"

    fd = sys.stdin.fileno()
    original_mode = termios.tcgetattr(fd)
    cursor_hidden = False
    menu_started = False
    try:
        try:
            menu_mode = list(original_mode)
            menu_mode[6] = list(original_mode[6])
            menu_mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
            menu_mode[6][termios.VMIN] = 1
            menu_mode[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSADRAIN, menu_mode)
            cursor_hidden = True
            sys.stdout.write("\x1b[?25l")
            sys.stdout.write(
                "Choose an account:\n"
                "Use ↑/↓ to move, Enter to select, Esc/Ctrl-C/Ctrl-Z to cancel.\n\n"
            )
            sys.stdout.write("\x1b[s")
            menu_started = True
            cursor = next(
                (index for index, profile in enumerate(profiles) if profile.name == selected),
                0,
            )
            render(cursor)
            while True:
                key = read_key(fd)
                if key == "enter":
                    return profiles[cursor].name
                if key == "cancel":
                    return None
                if key == "up":
                    cursor = (cursor - 1) % len(profiles)
                    render(cursor)
                elif key == "down":
                    cursor = (cursor + 1) % len(profiles)
                    render(cursor)
        finally:
            try:
                if menu_started:
                    sys.stdout.write("\x1b[u")
                    if len(profiles) > 1:
                        sys.stdout.write(f"\x1b[{len(profiles) - 1}B")
                    sys.stdout.write("\r\n")
                if cursor_hidden:
                    sys.stdout.write("\x1b[?25h")
                sys.stdout.flush()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, original_mode)
    except KeyboardInterrupt:
        # Also restore cleanly for an externally delivered SIGINT.
        return None


def _prepare_onboarding(store: ProfileStore, profile_name: str, binary: str) -> None:
    """Complete missing per-profile TUI state before a signed-in launch."""

    from .onboarding import needs_repair, repair_if_authenticated

    profile = store.get_fresh(profile_name)
    if not needs_repair(profile):
        return
    # auth status can take up to ten seconds while the first launcher holds
    # this lock. Give that repair time to finish before reporting contention.
    deadline = time.monotonic() + 12.0
    while True:
        try:
            with _profile_lock(store, profile_name, exclusive=True):
                profile = store.get_fresh(profile_name)
                repair_if_authenticated(profile, binary)
                return
        except RuntimeError as exc:
            if "currently in use" not in str(exc):
                raise
            if not needs_repair(store.get_fresh(profile_name)):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"profile '{profile_name}' needs one-time Claude setup repair; "
                    "exit its other Claude sessions and retry"
                ) from exc
            time.sleep(0.05)


def _native(store: ProfileStore, args: list[str]) -> int:
    """Run real Claude in this terminal with the currently selected login."""

    profile_name = store.selected().name
    profile = store.get_fresh(profile_name)
    if any(
        argument.split("=", 1)[0] in {"--bg", "--background"}
        for argument in args
    ):
        raise RuntimeError(
            "background Claude sessions are unavailable through ccs; "
            "run Claude in this terminal"
        )
    binary = claude_binary()
    if profile.config_dir is not None and _starts_native_session(args):
        _prepare_onboarding(store, profile_name, binary)

    with _profile_lock(store, profile_name, exclusive=False):
        profile = store.get_fresh(profile_name)
        environment = profile_environment(profile)
        forwarded = args
        if profile.config_dir is not None and _uses_interactive_customizations(args):
            from .shared_mcp_plugins import prepare_shared_mcp_plugins
            from .shared_settings import prepare_shared_settings

            settings_args, settings_env = prepare_shared_settings(store, profile)
            mcp_args, mcp_env = prepare_shared_mcp_plugins(store, profile, Path.cwd())
            environment.update(settings_env)
            environment.update(mcp_env)
            forwarded = [*settings_args, *mcp_args, *args]
        return run_passthrough(binary, forwarded, environment)


_NATIVE_ADMIN_COMMANDS = frozenset({
    "agents", "attach", "auth", "auto-mode", "config", "daemon", "doctor",
    "gateway", "import", "install", "kill", "logs", "mcp", "plugin",
    "plugins", "project", "remote-control", "respawn", "rm", "self-hosted-runner",
    "setup-token", "stop", "ultrareview", "update", "upgrade",
})


def _starts_native_session(args: list[str]) -> bool:
    if args and args[0] in _NATIVE_ADMIN_COMMANDS:
        return False
    if (
        len(args) >= 2
        and args[0] in {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"}
        and args[1] == "daemon"
    ):
        return False
    return not any(
        argument.split("=", 1)[0] in {"--help", "--version", "-h", "-v"}
        for argument in args
    )


def _uses_interactive_customizations(args: list[str]) -> bool:
    return _starts_native_session(args) and not any(
        argument.split("=", 1)[0] in {"--bare", "--safe-mode"}
        for argument in args
    )


def _legacy_session(store: ProfileStore, action: str, argv: list[str]) -> int:
    """Let users finish tmux sessions created by the old version."""

    from .tmux_sessions import TmuxSessions

    parser = argparse.ArgumentParser(prog=f"ccs {action}")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--session", help="old run ID or tmux session name")
    args = parser.parse_args(argv)
    cwd = (args.project or Path.cwd()).resolve()
    manager = TmuxSessions(store)
    candidates = manager.list_sessions_for_cwd(cwd)
    if action == "attach":
        candidates = [item for item in candidates if not item.get("dead")]
    if not candidates:
        raise RuntimeError(f"no {'active ' if action == 'attach' else ''}old tmux session for {cwd}")
    if args.session:
        selected = next(
            (
                item for item in candidates
                if item.get("run_id") == args.session or item.get("name") == args.session
            ),
            None,
        )
        if selected is None:
            raise RuntimeError(f"old session '{args.session}' was not found in {cwd}")
    elif len(candidates) == 1:
        selected = candidates[0]
    else:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise RuntimeError(
                f"multiple old sessions exist; use 'ccs {action} --session <run-id>'"
            )
        print(f"Choose a session to {action}:")
        for index, item in enumerate(candidates, start=1):
            state = "exited" if item.get("dead") else (
                "attached" if item.get("attached") else "detached"
            )
            print(f"  {index}. {item.get('run_id') or 'legacy'}  {state}  {item['name']}")
        print("  0. Cancel")
        while True:
            try:
                choice = input("Enter a session number: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not choice or choice == "0":
                print("Session selection canceled.")
                return 0
            if choice.isdecimal() and 1 <= int(choice) <= len(candidates):
                selected = candidates[int(choice) - 1]
                break
            print(f"Invalid choice. Enter a number from 1 to {len(candidates)}, or 0 to cancel.")
    target = str(selected["name"])
    result = (
        manager.attach(cwd, session_name=target)
        if action == "attach" else manager.stop(cwd, session_name=target)
    )
    if result == 0 and action == "stop" and isinstance(selected.get("run_id"), str):
        store.remove_background_session(str(selected["run_id"]))
    return result


def _usage(store: ProfileStore, args: argparse.Namespace) -> int:
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
                store.get_fresh(profile.name)
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

    live_enabled = not args.json and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccs", description="Select Claude Code accounts manually and view their usage"
    )
    parser.add_argument("--version", action="version", version=f"ccs {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="register your existing Claude Code account")
    init.add_argument("--name", default="main")
    add = commands.add_parser("add", help="add an isolated account profile")
    add.add_argument("name")
    add.add_argument("--email", help="prefill the Claude login page")
    add.add_argument("--no-login", action="store_true", help="sign in later with ccs login")
    login = commands.add_parser("login", help="sign in to a profile")
    login.add_argument("name")
    login.add_argument("--email")
    list_command = commands.add_parser("list", help="list profiles and login state")
    list_command.add_argument("--show-identity", action="store_true")
    switch = commands.add_parser("switch", help="choose the account for new Claude sessions")
    switch.add_argument("name", nargs="?", help="omit to pick from a list")
    usage = commands.add_parser("usage", help="show five-hour and weekly usage for all accounts")
    usage.add_argument("profiles", nargs="*", help="profile names (default: all)")
    usage.add_argument("--json", action="store_true")
    usage.add_argument("--timeout", type=float, default=45)
    remove = commands.add_parser("remove", help="remove an account profile")
    remove.add_argument("name")
    remove.add_argument("--purge-data", action="store_true")
    commands.add_parser("setup", help="install the simple Zsh wrapper and remove the old monitor")
    shell = commands.add_parser("shell", help="manage the Zsh claude wrapper")
    shell.add_argument("action", choices=("install", "uninstall", "status"))
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        # Keep old sessions accessible while no new invocation can create one.
        if argv and argv[0] in {"attach", "stop"}:
            return _legacy_session(ProfileStore(), argv[0], argv[1:])
        if argv and argv[0] == "service":
            if argv[1:] == ["uninstall"]:
                from . import service

                service.uninstall(ProfileStore())
                print("Removed the old background monitor.")
                return 0
            raise ValueError("background service commands were removed; use 'ccs setup'")
        if argv and argv[0] in {"native", "run", "resume"}:
            command, rest = argv[0], argv[1:]
            if command == "run":
                while rest and rest[0] in {"--no-auto", "--foreground"}:
                    rest = rest[1:]
                if rest and rest[0] != "--":
                    raise ValueError("old run options are unavailable; use 'claude' directly")
            if rest and rest[0] == "--":
                rest = rest[1:]
            if command == "resume":
                if len(rest) > 1:
                    raise ValueError("resume accepts at most one session ID")
                rest = ["--resume", rest[0]] if rest else ["--continue"]
            return _native(ProfileStore(), rest)
        if argv and argv[0] == "use":
            argv[0] = "switch"

        args = _parser().parse_args(argv)
        store = ProfileStore()
        if args.command == "init":
            profile = store.add_default(args.name)
            print(f"Registered existing Claude Code login as '{profile.name}'.")
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
                store.get_fresh(profile.name)
                return subprocess.call(login_args, env=profile_environment(profile))
        if args.command == "list":
            profiles = store.all()
            if not profiles:
                if not store.has_registry():
                    raise RuntimeError("no account profiles are configured; run 'ccs init'")
                print("No account profiles configured. Use 'ccs add <name>' or 'ccs init'.")
                return 0
            binary = claude_binary()
            selected = store.selected().name
            for profile in profiles:
                logged_in, method = auth_status(profile, binary)
                marker = "*" if profile.name == selected else " "
                state = "signed in" if logged_in else f"not ready ({method})"
                if args.show_identity and logged_in:
                    details = auth_details(profile, binary) or {}
                    email = _identity_text(details.get("email"), "unknown email")
                    org = _identity_text(details.get("orgName"))
                    state += f"  {email}" + (f"  [{org}]" if org else "")
                print(f"{marker} {profile.name:16} {state}")
            return 0
        if args.command == "switch":
            name = args.name or _choose_profile_name(store)
            if name is None:
                print("Account selection canceled.")
                return 0
            with _profile_lock(store, name, exclusive=False):
                profile = store.get_fresh(name)
                logged_in, method = auth_status(profile, claude_binary())
                if not logged_in:
                    raise RuntimeError(
                        f"profile '{name}' is not signed in with claude.ai "
                        f"(authentication: {method}); run 'ccs login {name}'"
                    )
                store.select(name)
            print(f"Selected '{name}'. New Claude sessions will use this account.")
            return 0
        if args.command == "usage":
            return _usage(store, args)
        if args.command == "remove":
            profile = store.get(args.name)
            with _profile_lock(store, profile.name, exclusive=True):
                fresh = store.get_fresh(profile.name)
                if fresh.config_dir != profile.config_dir:
                    raise RuntimeError("profile changed while preparing removal; retry")
                profile = fresh
                if profile.config_dir is None and args.purge_data:
                    raise ValueError(
                        "cannot purge the default profile because ~/.claude is shared; "
                        "use 'ccs remove <name>' to unregister and log out"
                    )
                registration_dir = store.profiles_dir / profile.name
                try:
                    registration_info = registration_dir.lstat()
                except OSError as exc:
                    raise ValueError("profile registration directory is missing or unsafe") from exc
                if (
                    not stat.S_ISDIR(registration_info.st_mode)
                    or registration_info.st_uid != os.getuid()
                    or stat.S_IMODE(registration_info.st_mode) != 0o700
                ):
                    raise ValueError("profile registration directory is missing or unsafe")
                archive = store.home / "removed"
                if archive.is_symlink():
                    raise RuntimeError(f"unsafe archive directory: {archive}")
                archive.mkdir(mode=0o700, exist_ok=True)
                info = archive.stat()
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
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
                was_selected = store.selected().name == profile.name
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                destination = archive / f"{profile.name}-{stamp}-{uuid.uuid4().hex[:8]}"
                if profile.config_dir is None:
                    destination = store.remove_default(
                        profile.name, archive_to=destination, purge_data=args.purge_data
                    )
                else:
                    destination = store.remove_managed(
                        profile.name, archive_to=destination, purge_data=args.purge_data
                    )
                if args.purge_data:
                    shutil.rmtree(destination)
                    store.finish_purge(destination)
                    if profile.config_dir is None:
                        print(
                            f"Removed '{profile.name}' and deleted its ccs registration data. "
                            "Shared Claude configuration and sessions were kept."
                        )
                    else:
                        print(f"Removed '{profile.name}' and deleted its local data.")
                else:
                    if profile.config_dir is None:
                        print(
                            f"Removed '{profile.name}'. Its ccs registration data was archived at "
                            f"{destination}. Shared Claude configuration and sessions were kept."
                        )
                    else:
                        print(f"Removed '{profile.name}'. Local history was archived at {destination}.")
                if was_selected:
                    remaining = store.all()
                    if remaining:
                        print(f"Selected '{store.selected().name}' for new Claude sessions.")
                    else:
                        print("No account profiles remain. Use 'ccs add <name>' or 'ccs init'.")
            return 0
        if args.command == "setup":
            if sys.platform == "darwin":
                from . import service

                service.uninstall(store)
            path = shell_integration.install(store)
            print(f"Zsh integration: {path}")
            print("Background monitor: removed")
            return 0
        if args.command == "shell":
            if args.action == "install":
                path = shell_integration.install(store)
                print(f"Installed Zsh integration in {path}. Open a new terminal or source it.")
            elif args.action == "uninstall":
                path = shell_integration.uninstall(store)
                print(f"Removed Zsh integration from {path}. Open a new terminal to apply it.")
            else:
                print("installed" if shell_integration.installed(store) else "not installed")
            return 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        return _error(str(exc))
    return _error("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
