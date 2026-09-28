"""Admission and lifetime locks for account-engine operations.

The state and locks live beside the purgeable engine directory. A consumer
must be admitted before it opens any per-slot lock under that directory, then
holds a shared lifetime lock until all compare-and-swap/stash work is done.
Purge closes admission first and drains the shared leases without holding the
account lock, then deletes the engine tree under that lock.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from claude_swap.exceptions import ConfigError, LockError
from claude_swap.locking import FileLock
from claude_swap.paths import get_backup_root

_STATE_VERSION = 1
_VALID_STATES = {"active", "closing", "purged"}


class AdmissionClosed(ConfigError):
    """The account engine is closing or has been purged."""


class EngineAdmissionGate:
    """Stable coordinator for purge versus long-running credential consumers."""

    def __init__(self, backup_root: Path | None = None):
        self.backup_root = Path(backup_root or get_backup_root()).absolute()
        self.home = self.backup_root.parents[2]
        expected = self.home / ".config" / "cc-swaper" / "account-engine"
        if self.backup_root != expected:
            raise ConfigError(f"Unexpected account-engine admission root: {self.backup_root}")
        self.parent = self.backup_root.parent
        self.coordinator_path = self.parent / ".account-engine.admission.lock"
        self.lifetime_path = self.parent / ".account-engine.consumers.lock"
        self.purge_path = self.parent / ".account-engine.purge.lock"
        self.state_path = self.parent / ".account-engine.state.json"

    def _ensure_parent(self) -> None:
        """Create only the private stable parent, never the purgeable root."""
        home = self.home
        try:
            home_info = home.lstat()
        except OSError as exc:
            raise ConfigError(f"Could not inspect account-engine home {home}: {exc}") from exc
        if stat.S_ISLNK(home_info.st_mode) or not stat.S_ISDIR(home_info.st_mode):
            raise ConfigError(f"Unsafe account-engine home: {home}")
        if hasattr(os, "getuid") and home_info.st_uid != os.getuid():
            raise ConfigError(f"Account-engine home has an unexpected owner: {home}")
        if stat.S_IMODE(home_info.st_mode) & 0o022:
            raise ConfigError(f"Account-engine home is writable by others: {home}")
        from claude_swap.paths import _no_dangerous_extended_acl

        if not _no_dangerous_extended_acl(home):
            raise ConfigError(f"Unsafe extended ACL on account-engine home: {home}")
        config = home / ".config"
        for directory, exact_mode in ((config, None), (self.parent, 0o700)):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                try:
                    os.mkdir(directory, 0o700)
                    info = directory.lstat()
                except OSError as exc:
                    raise ConfigError(
                        f"Could not create account-engine coordination directory {directory}: {exc}"
                    ) from exc
            except OSError as exc:
                raise ConfigError(
                    f"Could not inspect account-engine coordination directory {directory}: {exc}"
                ) from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ConfigError(
                    f"Unsafe account-engine coordination path: {directory} must be a real directory"
                )
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise ConfigError(
                    f"Unsafe account-engine coordination directory owner: {directory}"
                )
            mode = stat.S_IMODE(info.st_mode)
            if exact_mode is not None and mode != exact_mode:
                raise ConfigError(
                    f"Unsafe account-engine coordination permissions on {directory}: "
                    f"expected {exact_mode:04o}, found {mode:04o}"
                )
            if exact_mode is None and mode & 0o022:
                raise ConfigError(
                    f"Unsafe writable account-engine ancestor: {directory}"
                )
            if not _no_dangerous_extended_acl(directory):
                raise ConfigError(
                    f"Unsafe extended ACL grants access to account-engine coordination directory: {directory}"
                )
        # Do not call validate_backup_layout here: a switcher can retain a
        # temporary/forked home after the process environment has changed
        # (the harness does this). Validate the stable parent derived from its
        # stored root, without consulting the ambient HOME again.

    def _read_state(self) -> str:
        try:
            info = self.state_path.lstat()
        except FileNotFoundError:
            return "active"
        except OSError as exc:
            raise ConfigError(f"Could not inspect account-engine state: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ConfigError("Unsafe account-engine lifecycle state file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ConfigError("Account-engine lifecycle state has an unexpected owner")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ConfigError("Account-engine lifecycle state must have mode 0600")
        from claude_swap.paths import _no_dangerous_extended_acl

        if not _no_dangerous_extended_acl(self.state_path):
            raise ConfigError("Unsafe extended ACL on account-engine lifecycle state")
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Could not read account-engine lifecycle state: {exc}") from exc
        if (
            not isinstance(payload, dict)
            or type(payload.get("schemaVersion")) is not int
            or payload.get("schemaVersion") != _STATE_VERSION
            or payload.get("state") not in _VALID_STATES
        ):
            raise ConfigError("Invalid account-engine lifecycle state")
        return payload["state"]

    def _write_state(self, state: str) -> None:
        if state not in _VALID_STATES:
            raise ValueError(f"invalid account-engine state: {state}")
        payload = json.dumps({"schemaVersion": _STATE_VERSION, "state": state})
        try:
            try:
                info = self.state_path.lstat()
            except FileNotFoundError:
                info = None
            if info is not None and (
                stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
            ):
                raise ConfigError("Unsafe account-engine lifecycle state file")
            if info is not None:
                if hasattr(os, "getuid") and info.st_uid != os.getuid():
                    raise ConfigError(
                        "Account-engine lifecycle state has an unexpected owner"
                    )
                if stat.S_IMODE(info.st_mode) != 0o600:
                    raise ConfigError(
                        "Account-engine lifecycle state must have mode 0600"
                    )
                from claude_swap.paths import _no_dangerous_extended_acl

                if not _no_dangerous_extended_acl(self.state_path):
                    raise ConfigError(
                        "Unsafe extended ACL on account-engine lifecycle state"
                    )
            descriptor, temp_name = tempfile.mkstemp(
                prefix=".account-engine.state.", suffix=".tmp", dir=self.parent
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.state_path)
                os.chmod(self.state_path, 0o600, follow_symlinks=False)
            except BaseException:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise ConfigError(f"Could not persist account-engine lifecycle state: {exc}") from exc

    @contextmanager
    def operation(self, *, reactivate: bool = False) -> Iterator[None]:
        """Admit an operation and hold a shared lifetime lease through its work."""
        self._ensure_parent()
        lease: FileLock | None = None
        with FileLock(self.coordinator_path):
            state = self._read_state()
            if state == "active":
                lease = FileLock(self.lifetime_path, shared=True, timeout=0.25)
                if not lease.acquire():
                    raise AdmissionClosed("Account-engine purge is closing admission; retry the command.")
            elif state == "purged" and reactivate:
                exclusive = FileLock(self.lifetime_path, timeout=10.0)
                if not exclusive.acquire():
                    raise AdmissionClosed("Account-engine purge is still draining; retry the command.")
                try:
                    self._write_state("active")
                finally:
                    exclusive.release()
                lease = FileLock(self.lifetime_path, shared=True, timeout=0.25)
                if not lease.acquire():
                    # The coordinator is still held, so no new purge or
                    # admission can have changed state between these locks.
                    self._write_state("purged")
                    raise AdmissionClosed("Account-engine purge is still draining; retry the command.")
            else:
                raise AdmissionClosed(
                    "Account engine is closing or purged; retry after purge or add an account to reopen it."
                )
        assert lease is not None
        try:
            yield
        finally:
            lease.release()

    @contextmanager
    def purge(self) -> Iterator["PurgeAdmission"]:
        """Close admission and drain consumers without holding the account lock."""
        self._ensure_parent()
        with FileLock(self.purge_path, timeout=120.0):
            with FileLock(self.coordinator_path):
                self._write_state("closing")
            lifetime = FileLock(self.lifetime_path, timeout=120.0)
            if not lifetime.acquire():
                # Stay closed. A later purge invocation can safely retry.
                raise LockError(
                    "Timed out waiting for in-flight account refreshes to finish; "
                    "purge admission remains closed, so retry purge."
                )
            session = PurgeAdmission(self, lifetime)
            try:
                # A previous purge may have failed before deleting anything.
                # It can reopen admission before releasing its lease; close it
                # again now that this invocation owns the exclusive drain.
                with FileLock(self.coordinator_path):
                    if self._read_state() == "active":
                        self._write_state("closing")
                yield session
            except BaseException:
                session._finish()
                raise
            else:
                session._finish()


class PurgeAdmission:
    """State transition helper scoped to an exclusive lifetime lease."""

    def __init__(self, gate: EngineAdmissionGate, lifetime: FileLock):
        self.gate = gate
        self.lifetime = lifetime
        self.destructive = False
        self.completed = False
        self.finished = False

    def mark_destructive(self) -> None:
        self.destructive = True

    def complete(self) -> None:
        with FileLock(self.gate.coordinator_path):
            self.gate._write_state("purged")
        self.completed = True

    def _finish(self) -> None:
        if self.finished:
            return
        try:
            if not self.destructive and not self.completed:
                with FileLock(self.gate.coordinator_path):
                    self.gate._write_state("active")
        finally:
            self.lifetime.release()
            self.finished = True
