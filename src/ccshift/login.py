"""Add an account by signing in with a browser, without touching the live login.

``ccshift add`` captures the account Claude Code is signed in to, so adding a
second account used to mean ``/login`` in Claude Code first, which replaces the
live login. ``ccshift add --login`` instead runs Claude Code's own sign-in,
``claude auth login`` (``--sso`` for single sign-on), with ``CLAUDE_CONFIG_DIR``
pointed at a throwaway profile under the backup dir. The browser flow, the
OAuth client and the token exchange all stay Claude Code's. When it succeeds,
the account is captured from that profile the same way ``add`` captures the
live one (``add_account`` reads the profile ``CLAUDE_CONFIG_DIR`` names), and
the profile and its hashed Keychain item are deleted.

Never ``claude auth logout`` the staging profile: logging out can revoke the
refresh token server-side, and that token is exactly what the new slot holds.
Deleting the local copies revokes nothing.
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from ccshift.exceptions import ConfigError
from ccshift.session import delete_macos_keychain_entry

if TYPE_CHECKING:
    from ccshift.switcher import ClaudeAccountSwitcher

STAGING_DIR_NAME = "login-staging"
# Long enough to finish an SSO sign-in (identity provider, MFA) in the browser.
LOGIN_TIMEOUT_S = 15 * 60
# A staging profile this old belongs to a sign-in that was killed before it
# could clean up after itself.
_STALE_STAGING_S = 60 * 60


def login_and_add(
    switcher: ClaudeAccountSwitcher,
    *,
    sso: bool = False,
    email: str | None = None,
    alias: str | None = None,
    slot: int | None = None,
    assume_yes: bool = False,
    json_mode: bool = False,
    private: bool = False,
    browser: str | None = None,
    handoff_file: str | None = None,
    timeout_s: float = LOGIN_TIMEOUT_S,
) -> dict | None:
    """Sign in with ``claude auth login`` in a staging profile and add the account.

    Returns what :meth:`ClaudeAccountSwitcher.add_account` returns. In JSON mode
    Claude Code's own output goes to stderr and nothing is read from stdin, so
    the sign-in has to finish through the browser callback. ``private`` opens
    the sign-in page in a private window (``browser``: a bundle identifier, see
    :mod:`ccshift.private_browser`); ``handoff_file`` instead writes the page's
    URL there for the menu bar app to open in its own private sign-in window.
    """
    switcher._refuse_session_shell()
    claude = shutil.which("claude")
    if not claude:
        raise ConfigError(
            "Claude Code (the 'claude' command) was not found on PATH. "
            "Install Claude Code, then try again."
        )

    root = switcher.backup_dir / STAGING_DIR_NAME
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _sweep_stale_profiles(root)
    staging = root / secrets.token_hex(8)
    staging.mkdir(mode=0o700)

    try:
        with _sigterm_raises():
            opener = (
                _write_opener(staging, ["--handoff-file", handoff_file]) if handoff_file
                else _write_opener(staging, ["--browser", browser] if browser else []) if private
                else None
            )
            _run_claude_login(
                claude, staging, sso=sso, email=email, opener=opener,
                json_mode=json_mode, timeout_s=timeout_s,
            )
            with _profile_environment(staging):
                return switcher.add_account(
                    slot=slot, alias=alias, assume_yes=assume_yes, set_active=False,
                )
    finally:
        _discard_profile(staging)


def _run_claude_login(
    claude: str,
    staging: Path,
    *,
    sso: bool,
    email: str | None,
    opener: Path | None,
    json_mode: bool,
    timeout_s: float,
) -> None:
    command = [claude, "auth", "login"]
    if sso:
        command.append("--sso")
    if email:
        command += ["--email", email]

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(staging)
    # Secure storage follows this variable when it is defined, which would put
    # the new credential outside the staging profile.
    env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
    if opener is not None:
        # claude opens the sign-in page with `$BROWSER <url>`.
        env["BROWSER"] = str(opener)

    process = subprocess.Popen(
        command,
        env=env,
        cwd=str(staging),
        # A terminal user can paste the code if the browser callback fails; the
        # menu bar app cannot, so JSON mode waits for the callback only.
        stdin=subprocess.DEVNULL if json_mode else None,
        stdout=sys.stderr if json_mode else None,
    )
    try:
        returncode = process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _stop(process)
        raise ConfigError(
            f"Sign-in did not finish within {int(timeout_s // 60)} minutes. Nothing was changed."
        ) from None
    except BaseException:
        _stop(process)
        raise
    if returncode != 0:
        raise ConfigError(f"Sign-in did not complete (claude auth login exited {returncode}). Nothing was changed.")


def _write_opener(staging: Path, options: list[str]) -> Path:
    """A one-line ``$BROWSER`` program running :mod:`ccshift.private_browser`.

    Lives in the staging profile, so it goes away with it.
    """
    command = [sys.executable, "-m", "ccshift.private_browser", *options]
    opener = staging / "open-private"
    opener.write_text(f"#!/bin/sh\nexec {shlex.join(command)} \"$@\"\n")
    opener.chmod(0o700)
    return opener


@contextmanager
def _profile_environment(staging: Path) -> Iterator[None]:
    """Point ``CLAUDE_CONFIG_DIR`` at the staging profile for the capture."""
    saved = {key: os.environ.get(key) for key in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR")}
    os.environ["CLAUDE_CONFIG_DIR"] = str(staging)
    os.environ.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _sigterm_raises() -> Iterator[None]:
    """Turn SIGTERM into SystemExit so the staging profile is still cleaned up.

    The menu bar app stops a cancelled sign-in by sending SIGTERM to the
    process group; the default action would skip every ``finally``.
    """
    def handler(signum, frame):  # noqa: ARG001
        raise SystemExit(128 + signum)

    try:
        previous = signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _discard_profile(staging: Path) -> None:
    """Delete a staging profile and its hashed Keychain item (best effort)."""
    delete_macos_keychain_entry(staging)
    shutil.rmtree(staging, ignore_errors=True)


def _sweep_stale_profiles(root: Path) -> None:
    now = time.time()
    for entry in root.iterdir():
        try:
            if entry.is_dir() and now - entry.stat().st_mtime > _STALE_STAGING_S:
                _discard_profile(entry)
        except OSError:
            continue
