"""Private profile and session state for the Claude account switcher.

The profile store deliberately does not copy or inspect Claude credentials or
transcripts.  A managed profile only gets a private directory.  The
``shared_projects`` property records the user's existing Claude transcript
anchor for the runner, which can pass an absolute transcript path to
``--resume`` when it needs to continue work after a profile switch.
"""

from __future__ import annotations

import json
import fcntl
import os
import re
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Any


_STATE_VERSION = 1
_PROFILES_FILENAME = "profiles.json"
_SESSIONS_FILENAME = "sessions.json"
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


@dataclass(frozen=True)
class Profile:
    """A configured Claude account profile.

    ``config_dir`` is ``None`` for the default profile.  That is intentional:
    callers must omit ``CLAUDE_CONFIG_DIR`` for that profile so Claude uses the
    existing ``~/.claude`` login.  Managed profiles point at a private
    directory under the store home.
    """

    name: str
    config_dir: Path | None


@dataclass(frozen=True)
class SelectionSnapshot:
    """The exact profile-selection revision observed before a session starts."""

    name: str | None
    revision: int


def _default_store_home() -> Path:
    configured = os.environ.get("CC_SWAPER_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path("~/.config/cc-swaper").expanduser()


def _absolute_path(path: Path) -> Path:
    """Return an absolute path without requiring it to exist."""

    return path.expanduser().absolute()


def _ensure_private_dir(path: Path) -> None:
    """Create a private directory, or tighten an existing store directory."""

    if path.is_symlink():
        raise ValueError(f"refusing symlink where private directory is required: {path}")

    if path.exists():
        if not path.is_dir():
            raise NotADirectoryError(path)
    else:
        path.mkdir(parents=True, mode=0o700)

    # mkdir honours the process umask.  Explicitly apply the intended mode so
    # that an existing store created under a permissive umask is private too.
    os.chmod(path, 0o700)


def _read_json(path: Path) -> Any:
    if path.is_symlink():
        raise ValueError(f"refusing symlink metadata file: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON metadata in {path}") from exc


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON with an atomic replace and mode 0600.

    The temporary file lives beside the destination, so ``os.replace`` is an
    atomic operation on the same filesystem.  The directory sync makes the
    rename durable on filesystems that support fsync for directories.
    """

    temporary_path: Path | None = None
    temporary_fd: int | None = None
    try:
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        os.fchmod(temporary_fd, 0o600)
        with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
            temporary_fd = None
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, path)
        temporary_path = None
        os.chmod(path, 0o600)

        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            # Directory fsync is not available on every supported platform;
            # the atomic replace and file fsync still provide the key safety
            # properties.
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _validate_profile_name(name: str) -> str:
    if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name):
        raise ValueError(
            "profile name must start with an ASCII letter or digit and contain "
            "only ASCII letters, digits, '_' or '-': "
            f"{name!r}"
        )
    return name


def _cwd_key(cwd: Path) -> str:
    if not isinstance(cwd, Path):
        cwd = Path(cwd)
    # Resolve ``..`` and symlink aliases so the same project is not recorded
    # under multiple metadata keys.  ``strict=False`` keeps this usable for a
    # working directory that will be created shortly after the switch.
    return str(cwd.expanduser().resolve(strict=False))


def _validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
        raise ValueError("session_id must be a non-empty string without NUL bytes")
    return session_id


@contextmanager
def _metadata_lock(home: Path):
    """Serialize read/modify/replace across CLI processes."""

    path = home / ".metadata.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("unsafe metadata lock file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def _try_recovery_profile_lock(home: Path, name: str):
    """Defer crash recovery while a live remover still owns this profile."""

    lock_dir = home / "locks"
    if lock_dir.is_symlink():
        raise ValueError("unsafe profile lock directory")
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    dir_info = lock_dir.stat()
    if dir_info.st_uid != os.getuid() or stat.S_IMODE(dir_info.st_mode) & 0o077:
        raise ValueError("profile lock directory is not private")
    path = lock_dir / f"profile-{name}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("unsafe profile lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


class ProfileStore:
    """Persist account profiles and last-resumed session IDs.

    The store home defaults to ``$CC_SWAPER_HOME`` or
    ``~/.config/cc-swaper``.  It owns only that directory.  Claude's existing
    configuration and credentials remain outside the store and are never read
    or copied.
    """

    def __init__(self, home: Path | None = None):
        self.home = _absolute_path(Path(home) if home is not None else _default_store_home())
        _ensure_private_dir(self.home)

        self.profiles_dir = self.home / "profiles"
        _ensure_private_dir(self.profiles_dir)

        self._state_path = self.home / _PROFILES_FILENAME
        self._sessions_path = self.home / _SESSIONS_FILENAME
        self._pending_removal_path = self.home / "pending-removal.json"
        self.shared_projects = _absolute_path(Path.home() / ".claude" / "projects")
        with _metadata_lock(self.home):
            self._recover_pending_removal()
            self._state = self._load_state()

    def add_default(self, name: str) -> Profile:
        """Create the first profile using Claude's existing default login."""

        name = _validate_profile_name(name)
        with _metadata_lock(self.home):
            self._state = self._load_state()
            profiles = self._state["profiles"]
            if profiles:
                raise ValueError("the default profile must be added first")
            return self._add_profile(name, kind="default")

    def add_managed(self, name: str) -> Profile:
        """Create a managed profile with an isolated config directory."""

        name = _validate_profile_name(name)
        with _metadata_lock(self.home):
            self._state = self._load_state()
            profiles = self._state["profiles"]
            if not profiles:
                raise ValueError("add_default must be called before add_managed")
            if any(record["name"] == name for record in profiles):
                raise ValueError(f"profile already exists: {name}")
            return self._add_profile(name, kind="managed")

    def remove_managed(
        self, name: str, *, archive_to: Path | None = None, purge_data: bool = False
    ) -> Path:
        """Unregister a managed profile, optionally moving its data first.

        `archive_to` must be a new path in this store's private `removed/`
        directory. A journal lets the next process restore the source path if
        a crash happens after the move but before the registry update.
        """

        name = _validate_profile_name(name)
        if purge_data and archive_to is None:
            raise ValueError("purge_data requires an archive destination")
        with _metadata_lock(self.home):
            # A long-lived ProfileStore may have stale in-memory state while
            # another process adds/selects a profile.  Reload while holding
            # the same lock used by every metadata mutation.
            self._state = self._load_state()
            record = next(
                (record for record in self._state["profiles"] if record["name"] == name),
                None,
            )
            if record is None:
                raise KeyError(name)
            if record["kind"] != "managed":
                raise ValueError("the default profile cannot be removed")

            config_dir = Path(record["config_dir"])
            # _load_state validates this path, including ownership, mode,
            # containment, and symlink checks.  Re-check the object we return
            # so a stale or tampered state cannot turn removal into a path
            # handoff for an unsafe directory.
            if config_dir.is_symlink() or not config_dir.is_dir():
                raise ValueError(f"unsafe or missing config directory for {name}")
            config_info = config_dir.stat()
            if config_info.st_uid != os.getuid() or stat.S_IMODE(config_info.st_mode) & 0o077:
                raise ValueError(f"managed config directory is not private for {name}")
            if not config_dir.resolve().is_relative_to(self.profiles_dir.resolve()):
                raise ValueError(f"config directory escapes profile store for {name}")

            destination: Path | None = None
            if archive_to is not None:
                destination = _absolute_path(archive_to)
                archive_root = self.home / "removed"
                if destination.parent != archive_root or archive_root.is_symlink():
                    raise ValueError("archive destination must be inside the private removed directory")
                if not re.fullmatch(rf"{re.escape(name)}-\d{{8}}T\d{{6}}Z-[0-9a-f]{{8}}", destination.name):
                    raise ValueError("archive destination has an invalid generated name")
                if not archive_root.is_dir():
                    raise ValueError("archive directory is missing")
                archive_info = archive_root.stat()
                if archive_info.st_uid != os.getuid() or stat.S_IMODE(archive_info.st_mode) & 0o077:
                    raise ValueError("archive directory is not private")
                if destination.exists() or destination.is_symlink():
                    raise FileExistsError(destination)
                if self._pending_removal_path.exists() or self._pending_removal_path.is_symlink():
                    raise RuntimeError("another profile removal needs recovery")

            if self._sessions_path.is_symlink():
                raise ValueError(f"refusing symlink metadata file: {self._sessions_path}")
            sessions_exist = self._sessions_path.exists()
            sessions = self._load_sessions() if sessions_exist else {}
            remaining_sessions = {
                cwd: session
                for cwd, session in sessions.items()
                if not (isinstance(session, dict) and session.get("profile") == name)
            }

            next_state = self._copy_state()
            next_state["profiles"] = [
                profile
                for profile in next_state["profiles"]
                if profile["name"] != name
            ]
            if next_state.get("selected") == name:
                next_state["selected"] = (
                    next_state["profiles"][0]["name"] if next_state["profiles"] else None
                )
                next_state["selection_revision"] += 1

            if destination is not None:
                _write_json_atomic(
                    self._pending_removal_path,
                    {
                        "version": _STATE_VERSION,
                        "name": name,
                        "source": str(config_dir),
                        "destination": str(destination),
                        "sessions_existed": sessions_exist,
                        "sessions_before": sessions,
                        "purge": purge_data,
                    },
                )
            moved = False
            sessions_changed = False
            try:
                if destination is not None:
                    config_dir.rename(destination)
                    moved = True
                # Write sessions first so no committed state can retain a
                # session pointing at an unregistered profile.
                if remaining_sessions != sessions:
                    _write_json_atomic(
                        self._sessions_path,
                        {"version": _STATE_VERSION, "sessions": remaining_sessions},
                    )
                    sessions_changed = True
                self._write_state(next_state)
            except BaseException:
                # Atomic replace may have committed before a later fsync or
                # chmod failed. In that case leave the archived directory and
                # journal for startup recovery instead of restoring a path
                # that the registry no longer knows about.
                try:
                    on_disk = _read_json(self._state_path)
                    profiles_on_disk = on_disk.get("profiles") if isinstance(on_disk, dict) else None
                    committed = isinstance(profiles_on_disk, list) and not any(
                        isinstance(item, dict) and item.get("name") == name
                        for item in profiles_on_disk
                    )
                except (OSError, ValueError):
                    committed = False
                if moved and committed:
                    raise
                rollback_ok = True
                if moved:
                    try:
                        destination.rename(config_dir)  # type: ignore[union-attr]
                    except OSError:
                        rollback_ok = False
                if sessions_changed:
                    try:
                        _write_json_atomic(
                            self._sessions_path,
                            {"version": _STATE_VERSION, "sessions": sessions},
                        )
                    except OSError:
                        rollback_ok = False
                if rollback_ok and destination is not None:
                    self._pending_removal_path.unlink(missing_ok=True)
                raise
            self._state = next_state
            if destination is not None and not purge_data:
                # If this unlink fails, recovery sees an unregistered profile
                # at the destination and only removes the stale journal.
                try:
                    self._pending_removal_path.unlink()
                except OSError:
                    pass
            return destination or config_dir

    def finish_purge(self, destination: Path) -> None:
        """Clear a purge journal only after its archived directory is gone."""

        with _metadata_lock(self.home):
            payload = _read_json(self._pending_removal_path)
            if not isinstance(payload, dict) or payload.get("purge") is not True:
                raise ValueError("no matching purge is pending")
            if payload.get("destination") != str(destination):
                raise ValueError("purge destination does not match the journal")
            if destination.exists() or destination.is_symlink():
                raise RuntimeError("purge destination still exists")
            self._pending_removal_path.unlink()

    def _recover_pending_removal(self) -> None:
        pending = self._pending_removal_path
        if not pending.exists() and not pending.is_symlink():
            return
        info = pending.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("profile-removal journal is not private")
        payload = _read_json(pending)
        if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
            raise ValueError("invalid profile-removal journal")
        name = _validate_profile_name(payload.get("name"))
        raw_source = payload.get("source")
        raw_destination = payload.get("destination")
        if not isinstance(raw_source, str) or not isinstance(raw_destination, str):
            raise ValueError("profile-removal journal has invalid paths")
        source = Path(raw_source)
        destination = Path(raw_destination)
        if source != self.profiles_dir / name or destination.parent != self.home / "removed":
            raise ValueError("profile-removal journal has unsafe paths")
        if not re.fullmatch(rf"{re.escape(name)}-\d{{8}}T\d{{6}}Z-[0-9a-f]{{8}}", destination.name):
            raise ValueError("profile-removal journal has an invalid destination name")
        purge = payload.get("purge", False)
        if not isinstance(purge, bool):
            raise ValueError("profile-removal journal has an invalid purge flag")
        archive_root = destination.parent
        if archive_root.is_symlink() or not archive_root.is_dir():
            raise ValueError("profile-removal archive directory is unsafe")
        archive_info = archive_root.stat()
        if archive_info.st_uid != os.getuid() or stat.S_IMODE(archive_info.st_mode) & 0o077:
            raise ValueError("profile-removal archive directory is not private")
        if source.is_symlink() or destination.is_symlink():
            raise ValueError("profile-removal journal points at a symlink")
        with _try_recovery_profile_lock(self.home, name) as acquired:
            if not acquired:
                return
            state = _read_json(self._state_path)
            if not isinstance(state, dict) or not isinstance(state.get("profiles"), list):
                raise ValueError("cannot recover profile removal from invalid metadata")
            registered = any(
                isinstance(record, dict) and record.get("name") == name
                for record in state["profiles"]
            )
            if registered and destination.is_dir() and not source.exists():
                destination.rename(source)
            elif registered and source.is_dir() and not destination.exists():
                pass
            elif not registered and destination.is_dir() and not source.exists():
                if purge:
                    shutil.rmtree(destination)
            elif not registered and purge and not destination.exists() and not source.exists():
                pass
            else:
                raise RuntimeError("profile removal needs manual recovery")
            if registered and "sessions_before" in payload:
                previous = payload["sessions_before"]
                existed = payload.get("sessions_existed")
                if not isinstance(previous, dict) or not isinstance(existed, bool):
                    raise ValueError("profile-removal journal has invalid session backup")
                if existed:
                    _write_json_atomic(
                        self._sessions_path,
                        {"version": _STATE_VERSION, "sessions": previous},
                    )
                elif self._sessions_path.exists():
                    self._sessions_path.unlink()
            pending.unlink()

    def get(self, name: str) -> Profile:
        name = _validate_profile_name(name)
        for record in self._state["profiles"]:
            if record["name"] == name:
                return self._profile_from_record(record)
        raise KeyError(name)

    def all(self) -> list[Profile]:
        """Return profiles in their insertion order."""

        return [self._profile_from_record(record) for record in self._state["profiles"]]

    def selected(self) -> Profile:
        selected_name = self._state.get("selected")
        if selected_name is None:
            raise RuntimeError("no profile has been configured")
        return self.get(selected_name)

    def select(self, name: str) -> Profile:
        name = _validate_profile_name(name)
        with _metadata_lock(self.home):
            self._state = self._load_state()
            profile = self.get(name)
            next_state = self._copy_state()
            next_state["selected"] = name
            next_state["selection_revision"] += 1
            self._write_state(next_state)
            self._state = next_state
            return profile

    def selection_snapshot(self) -> SelectionSnapshot:
        """Read a selection token while holding the metadata lock."""

        with _metadata_lock(self.home):
            self._state = self._load_state()
            if not self._state["_selection_revision_present"]:
                # Upgrade old metadata before issuing a token. A still-running
                # older CLI drops this field on its next write, which then
                # makes the conditional selection fail closed.
                upgraded = self._copy_state()
                upgraded["selection_revision"] = secrets.randbits(63) or 1
                self._write_state(upgraded)
                self._state = upgraded
            return SelectionSnapshot(
                self._state.get("selected"),
                self._state["selection_revision"],
            )

    def select_if_unchanged(self, snapshot: SelectionSnapshot, name: str) -> bool:
        """Apply a delayed session selection without overwriting a newer choice."""

        name = _validate_profile_name(name)
        with _metadata_lock(self.home):
            self._state = self._load_state()
            if (
                not self._state["_selection_revision_present"]
                or self._state.get("selected") != snapshot.name
                or self._state["selection_revision"] != snapshot.revision
            ):
                return False
            self.get(name)
            next_state = self._copy_state()
            next_state["selected"] = name
            next_state["selection_revision"] += 1
            self._write_state(next_state)
            self._state = next_state
            return True

    def last_session(self, cwd: Path) -> str | None:
        """Return the last session recorded for ``cwd``, if any."""

        with _metadata_lock(self.home):
            self._state = self._load_state()
            if self._sessions_path.is_symlink():
                raise ValueError(f"refusing symlink metadata file: {self._sessions_path}")
            if not self._sessions_path.exists():
                return None
            sessions = self._load_sessions()
            record = sessions.get(_cwd_key(cwd))
            return record["id"] if isinstance(record, dict) else record

    def last_transcript(self, cwd: Path) -> tuple[str, Path] | None:
        """Return the verified recorded owner and path when a hook supplied it."""

        with _metadata_lock(self.home):
            self._state = self._load_state()
            if self._sessions_path.is_symlink():
                raise ValueError(f"refusing symlink metadata file: {self._sessions_path}")
            if not self._sessions_path.exists():
                return None
            record = self._load_sessions().get(_cwd_key(cwd))
            if not isinstance(record, dict):
                return None
            return record["profile"], Path(record["path"])

    def set_last_session(
        self,
        cwd: Path,
        session_id: str,
        *,
        profile_name: str | None = None,
        transcript_path: Path | None = None,
    ) -> None:
        """Record the session used to resume work in ``cwd``.

        Session IDs are intentionally global to the store rather than tied to
        a profile.  This lets the runner restart Claude under the next account
        with the same ``--resume`` ID after a usage limit is reached.
        """

        key = _cwd_key(cwd)
        session_id = _validate_session_id(session_id)
        if (profile_name is None) != (transcript_path is None):
            raise ValueError("profile_name and transcript_path must be provided together")
        with _metadata_lock(self.home):
            # The lock must cover load, merge, and atomic replace together;
            # replacing the JSON file atomically alone still loses a sibling
            # project's update when two writers start from the same snapshot.
            self._state = self._load_state()
            if profile_name is not None:
                self.get(profile_name)
                assert transcript_path is not None
                if not transcript_path.is_absolute():
                    raise ValueError("transcript_path must be absolute")
            if self._sessions_path.is_symlink():
                raise ValueError(f"refusing symlink metadata file: {self._sessions_path}")
            sessions = self._load_sessions() if self._sessions_path.exists() else {}
            sessions[key] = (
                {"id": session_id, "profile": profile_name, "path": str(transcript_path)}
                if profile_name is not None
                else session_id
            )
            _write_json_atomic(
                self._sessions_path,
                {"version": _STATE_VERSION, "sessions": sessions},
            )

    def _add_profile(self, name: str, *, kind: str) -> Profile:
        profile_dir = self.profiles_dir / name
        # ``exists`` follows symlinks, so check ``is_symlink`` separately to
        # reject dangling links as conflicting paths too.
        if profile_dir.exists() or profile_dir.is_symlink():
            raise FileExistsError(f"profile path already exists: {profile_dir}")

        profile_dir.mkdir(mode=0o700)
        os.chmod(profile_dir, 0o700)

        config_dir: Path | None = None if kind == "default" else profile_dir
        record = {
            "name": name,
            "kind": kind,
            "config_dir": None if config_dir is None else str(config_dir),
            "projects_path": str(self.shared_projects),
        }
        next_state = self._copy_state()
        next_state["profiles"].append(record)
        if next_state.get("selected") is None:
            next_state["selected"] = name
            next_state["selection_revision"] += 1
        self._write_state(next_state)
        self._state = next_state
        return Profile(name=name, config_dir=config_dir)

    def _copy_state(self) -> dict[str, Any]:
        return {
            "version": self._state["version"],
            "shared_projects": self._state["shared_projects"],
            "profiles": [dict(record) for record in self._state["profiles"]],
            "selected": self._state.get("selected"),
            "selection_revision": self._state["selection_revision"],
        }

    def _load_state(self) -> dict[str, Any]:
        if self._state_path.is_symlink():
            raise ValueError(f"refusing symlink metadata file: {self._state_path}")
        if not self._state_path.exists():
            return {
                "version": _STATE_VERSION,
                "shared_projects": str(self.shared_projects),
                "profiles": [],
                "selected": None,
                "selection_revision": 0,
                "_selection_revision_present": True,
            }

        payload = _read_json(self._state_path)
        if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
            raise ValueError(f"unsupported profile metadata in {self._state_path}")
        if payload.get("shared_projects") != str(self.shared_projects):
            raise ValueError("profile metadata points at a different shared projects directory")

        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list):
            raise ValueError("profile metadata has an invalid profiles list")
        profiles: list[dict[str, Any]] = []
        names: set[str] = set()
        for raw_record in raw_profiles:
            if not isinstance(raw_record, dict):
                raise ValueError("profile metadata contains an invalid record")
            name = _validate_profile_name(raw_record.get("name"))
            if name in names:
                raise ValueError(f"duplicate profile in metadata: {name}")
            names.add(name)
            kind = raw_record.get("kind")
            if kind not in {"default", "managed"}:
                raise ValueError(f"invalid profile kind for {name}: {kind!r}")
            expected_config = None if kind == "default" else str(self.profiles_dir / name)
            if raw_record.get("config_dir") != expected_config:
                raise ValueError(f"invalid config directory for profile {name}")
            if expected_config is not None:
                config_path = Path(expected_config)
                if config_path.is_symlink() or not config_path.is_dir():
                    raise ValueError(f"unsafe or missing config directory for profile {name}")
                config_stat = config_path.stat()
                if config_stat.st_uid != os.getuid() or stat.S_IMODE(config_stat.st_mode) & 0o077:
                    raise ValueError(f"managed config directory is not private for {name}")
                if not config_path.resolve().is_relative_to(self.profiles_dir.resolve()):
                    raise ValueError(f"config directory escapes profile store for {name}")
                if (config_path / "projects").is_symlink():
                    raise ValueError(f"managed projects directory is a symlink for {name}")
            if raw_record.get("projects_path") != str(self.shared_projects):
                raise ValueError(f"invalid projects directory for profile {name}")
            profiles.append(
                {
                    "name": name,
                    "kind": kind,
                    "config_dir": expected_config,
                    "projects_path": str(self.shared_projects),
                }
            )

        selected = payload.get("selected")
        selection_revision = payload.get("selection_revision", 0)
        if (
            isinstance(selection_revision, bool)
            or not isinstance(selection_revision, int)
            or selection_revision < 0
        ):
            raise ValueError("profile metadata has an invalid selection revision")
        if selected is not None and selected not in names:
            raise ValueError("selected profile is not present in metadata")
        if profiles and selected is None:
            raise ValueError("profile metadata has profiles but no selected profile")
        if profiles and profiles[0]["kind"] != "default":
            raise ValueError("the default profile must be first in metadata")
        default_count = sum(record["kind"] == "default" for record in profiles)
        if default_count != (1 if profiles else 0):
            raise ValueError("profile metadata must contain exactly one default profile")
        return {
            "version": _STATE_VERSION,
            "shared_projects": str(self.shared_projects),
            "profiles": profiles,
            "selected": selected,
            "selection_revision": selection_revision,
            "_selection_revision_present": "selection_revision" in payload,
        }

    def _load_sessions(self) -> dict[str, str | dict[str, str]]:
        payload = _read_json(self._sessions_path)
        if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
            raise ValueError(f"unsupported session metadata in {self._sessions_path}")
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            raise ValueError("session metadata has an invalid sessions map")
        validated: dict[str, str | dict[str, str]] = {}
        for cwd, session_record in sessions.items():
            if not isinstance(cwd, str):
                raise ValueError("session metadata contains a non-string cwd")
            if isinstance(session_record, dict):
                session_id = _validate_session_id(session_record.get("id"))
                profile_name = _validate_profile_name(session_record.get("profile"))
                if profile_name not in {record["name"] for record in self._state["profiles"]}:
                    raise ValueError("session metadata names an unknown profile")
                raw_path = session_record.get("path")
                if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                    raise ValueError("session metadata has an invalid transcript path")
                validated[cwd] = {"id": session_id, "profile": profile_name, "path": raw_path}
            else:
                validated[cwd] = _validate_session_id(session_record)
        return validated

    def _profile_from_record(self, record: dict[str, Any]) -> Profile:
        config_dir = record["config_dir"]
        return Profile(
            name=record["name"],
            config_dir=None if config_dir is None else Path(config_dir),
        )

    def _write_state(self, state: dict[str, Any]) -> None:
        if self._state_path.is_symlink():
            raise ValueError(f"refusing symlink metadata file: {self._state_path}")
        _write_json_atomic(self._state_path, state)


__all__ = ["Profile", "ProfileStore"]
