"""Claude Code integration: live usage feed and rate-limit hook.

Polling ``/api/oauth/usage`` is budgeted, so a polled reading is minutes old
and, after a 429, up to an hour and a quarter old. Claude Code already knows
the current numbers — every API response carries them — and exposes them two
ways this module plugs into:

``ccshift statusline-feed [--then CMD]``
    Configured as (a wrapper around) the ``statusLine`` command. Claude Code
    pipes its session JSON in, including ``rate_limits.{five_hour,seven_day}``;
    the reading is attributed to an account and recorded in the usage store
    (``UsageStore.feed_live``), then the JSON is handed unchanged to ``CMD`` so
    the user's own status line keeps rendering. The feed is best effort and
    must never delay or break the status line: it runs beside ``CMD``, and any
    failure is swallowed.

``ccshift limit-hit``
    Configured as the ``StopFailure`` hook for ``rate_limit``. A session just
    stopped because it hit a limit — the one moment a reading is certainly
    wrong — so an auto-switch tick starts immediately, detached, instead of
    waiting for the next scheduled poll.

``ccshift hooks install|uninstall|status`` edits Claude Code's
``settings.json`` to wire both in (keeping a backup, and the original status
line command so ``uninstall`` restores it exactly).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ccshift import live_usage, paths
from ccshift.usage_store import Identity, UsageStore

FEED_VERB = "statusline-feed"
LIMIT_VERB = "limit-hit"
# Two agents hitting the limit in the same breath fire two hooks; one tick
# decides for both.
LIMIT_HIT_DEBOUNCE_S = 20.0
INSTALL_RECORD = "hooks_install.json"
SETTINGS_BACKUP_SUFFIX = ".ccshift-bak"


# -- feed ---------------------------------------------------------------------


def read_identities(backup_root: Path) -> dict[str, Identity]:
    """slot → ``(email, organizationUuid)`` straight from ``sequence.json``.

    Deliberately not through ``ClaudeAccountSwitcher``: this runs on every
    status-line refresh, and constructing the switcher touches migrations,
    logging and credential stores this path has no use for.
    """
    try:
        data = json.loads((backup_root / "sequence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        return {}
    return {
        str(num): (info.get("email", ""), info.get("organizationUuid", "") or "")
        for num, info in accounts.items()
        if isinstance(info, dict)
    }


def feed_payload(raw: bytes | str, backup_root: Path | None = None) -> str | None:
    """Record one statusLine payload; the slot it landed on, or None.

    Never raises: the caller is a status line.
    """
    try:
        reading = live_usage.parse_statusline(json.loads(raw))
        if reading is None:
            return None
        root = backup_root or paths.get_backup_root()
        identities = read_identities(root)
        if not identities:
            return None
        return UsageStore(root / "cache").feed_live(reading, identities)
    except Exception:
        return None


def statusline_feed_main(argv: list[str]) -> int:
    """``ccshift statusline-feed [--then CMD]`` (see module docstring)."""
    then: str | None = None
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--then" and rest:
            then = rest.pop(0)
        elif arg.startswith("--then="):
            then = arg.split("=", 1)[1]
        # Anything else is ignored: a status line must not die on a flag.
    raw = b"" if sys.stdin is None or sys.stdin.closed else sys.stdin.buffer.read()

    child: subprocess.Popen | None = None
    if then:
        try:
            child = subprocess.Popen(then, shell=True, stdin=subprocess.PIPE)
        except OSError:
            child = None
    if child is not None and child.stdin is not None:
        try:
            child.stdin.write(raw)
            child.stdin.close()
        except OSError:
            pass  # the child exited without reading; its status is below
    feed_payload(raw)  # beside the child, not before it
    if child is None:
        return 0
    return child.wait()


# -- rate-limit hook ----------------------------------------------------------


def limit_hit_main(argv: list[str]) -> int:
    """``ccshift limit-hit``: start an immediate, detached auto-switch tick."""
    del argv
    try:
        payload = None
        if sys.stdin is not None and not sys.stdin.closed and not sys.stdin.isatty():
            payload = json.loads(sys.stdin.read() or "null")
        if isinstance(payload, dict):
            kind = payload.get("error_type")
            if isinstance(kind, str) and kind != "rate_limit":
                return 0
        root = paths.get_backup_root()
        stamp = root / ".limit-hit.stamp"
        try:
            if time.time() - stamp.stat().st_mtime < LIMIT_HIT_DEBOUNCE_S:
                return 0
        except OSError:
            pass
        try:
            root.mkdir(parents=True, exist_ok=True)
            stamp.touch()
        except OSError:
            pass
        subprocess.Popen(
            [sys.executable, "-m", "ccshift", "auto", "--once", "--json", "--limit-hit"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        pass  # observational hook: nothing it does may disturb Claude Code
    return 0


# -- settings.json wiring -----------------------------------------------------


def ccshift_executable() -> str:
    """Absolute command that runs this ccshift (hooks run with a bare PATH)."""
    found = shutil.which("ccshift")
    if found:
        return str(Path(found).absolute())
    return f"{shlex.quote(sys.executable)} -m ccshift"


def claude_settings_path() -> Path:
    return paths.get_claude_config_home() / "settings.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _dump(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _limit_hook_entries(settings: dict) -> list[dict]:
    hooks = settings.get("hooks")
    entries = hooks.get("StopFailure") if isinstance(hooks, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _is_ours(command: object, verb: str) -> bool:
    return isinstance(command, str) and f" {verb}" in f" {command}"


def hooks_status(settings: dict) -> dict:
    status_cmd = (settings.get("statusLine") or {}).get("command")
    return {
        "statusLine": _is_ours(status_cmd, FEED_VERB),
        "hasStatusLine": isinstance(status_cmd, str),
        "stopFailure": any(
            _is_ours(h.get("command"), LIMIT_VERB)
            for entry in _limit_hook_entries(settings)
            for h in entry.get("hooks", [])
            if isinstance(h, dict)
        ),
    }


def install_hooks(
    settings_path: Path | None = None,
    record_dir: Path | None = None,
    executable: str | None = None,
) -> list[str]:
    """Wire the feed and the hook into Claude Code's settings.json.

    Returns human-readable lines describing what changed. Idempotent. A user
    with no status line gets only the hook: adding a status line where there
    was none would take Claude Code's footer hints away.
    """
    path = settings_path or claude_settings_path()
    root = record_dir or paths.get_backup_root()
    exe = executable or ccshift_executable()
    settings = _load(path)
    changes: list[str] = []
    before = hooks_status(settings)

    if not before["statusLine"]:
        status = settings.get("statusLine")
        original = status.get("command") if isinstance(status, dict) else None
        if isinstance(original, str) and original.strip():
            record = _read_record(root)
            record["statusLineCommand"] = original
            _write_record(root, record)
            status["command"] = f"{exe} {FEED_VERB} --then {shlex.quote(original)}"
            changes.append("statusLine: now feeds live usage, then runs your command")
        else:
            changes.append(
                "statusLine: none configured, left alone (live feed needs one; "
                "the rate-limit hook still works)"
            )

    if not before["stopFailure"]:
        hooks = settings.setdefault("hooks", {})
        hooks.setdefault("StopFailure", []).append(
            {
                "matcher": "rate_limit",
                "hooks": [
                    {"type": "command", "command": f"{exe} {LIMIT_VERB}", "timeout": 15}
                ],
            }
        )
        changes.append("hooks.StopFailure[rate_limit]: switch immediately on a limit hit")

    if changes and any(not c.startswith("statusLine: none") for c in changes):
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + SETTINGS_BACKUP_SUFFIX))
        path.parent.mkdir(parents=True, exist_ok=True)
        _dump(path, settings)
    return changes


def uninstall_hooks(
    settings_path: Path | None = None, record_dir: Path | None = None
) -> list[str]:
    path = settings_path or claude_settings_path()
    root = record_dir or paths.get_backup_root()
    settings = _load(path)
    changes: list[str] = []

    status = settings.get("statusLine")
    command = status.get("command") if isinstance(status, dict) else None
    if _is_ours(command, FEED_VERB):
        original = _read_record(root).get("statusLineCommand")
        if not isinstance(original, str):
            # No record: recover it from the wrapper we wrote.
            try:
                parts = shlex.split(command)
                original = parts[parts.index("--then") + 1]
            except (ValueError, IndexError):
                original = None
        if isinstance(original, str):
            status["command"] = original
            changes.append("statusLine: restored your original command")
        else:
            settings.pop("statusLine", None)
            changes.append("statusLine: removed (original command not recoverable)")

    hooks = settings.get("hooks")
    if isinstance(hooks, dict) and isinstance(hooks.get("StopFailure"), list):
        removed = False
        kept: list = []
        for entry in hooks["StopFailure"]:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            inner = []
            for h in entry.get("hooks", []):
                if isinstance(h, dict) and _is_ours(h.get("command"), LIMIT_VERB):
                    removed = True
                else:
                    inner.append(h)
            if inner:
                kept.append({**entry, "hooks": inner})
        if removed:
            changes.append("hooks.StopFailure: removed the ccshift hook")
            if kept:
                hooks["StopFailure"] = kept
            else:
                hooks.pop("StopFailure", None)
                if not hooks:
                    settings.pop("hooks", None)

    if changes:
        shutil.copy2(path, path.with_name(path.name + SETTINGS_BACKUP_SUFFIX))
        _dump(path, settings)
    return changes


def _read_record(root: Path) -> dict:
    try:
        data = json.loads((root / INSTALL_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_record(root: Path, record: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / INSTALL_RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def hooks_main(argv: list[str]) -> int:
    """``ccshift hooks install|uninstall|status``."""
    action = argv[1] if len(argv) > 1 else "status"
    try:
        if action == "install":
            changes = install_hooks()
            print("\n".join(changes) if changes else "Already installed.")
        elif action == "uninstall":
            changes = uninstall_hooks()
            print("\n".join(changes) if changes else "Nothing to remove.")
        elif action == "status":
            print(json.dumps(hooks_status(_load(claude_settings_path())), indent=2))
        else:
            print("usage: ccshift hooks [install|uninstall|status]", file=sys.stderr)
            return 2
    except (OSError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


def dispatch(argv: list[str]) -> int:
    verb = argv[0]
    if verb == FEED_VERB:
        return statusline_feed_main(argv[1:])
    if verb == LIMIT_VERB:
        return limit_hit_main(argv[1:])
    return hooks_main(argv)
