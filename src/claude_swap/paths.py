"""Path resolution for Claude Code config and credential files.

Mirrors claude-code's own resolution so cswap reads and writes the same files
claude-code does. Key rules (from claude-code source):

- Config home: ``CLAUDE_CONFIG_DIR`` if set, else ``~/.claude``.
- Global config: ``<config_home>/.config.json`` if it exists (legacy),
  otherwise ``(CLAUDE_CONFIG_DIR || $HOME)/.claude.json``. Note the asymmetry:
  ``.claude.json`` sits at homedir by default, not inside ``.claude/``.
- Credentials: ``<config_home>/.credentials.json``.

Also resolves the bundled account engine's private data root under
``~/.config/cc-swaper/account-engine``. The historical upstream
``~/.claude-swap-backup`` path is identified only for compatibility tests and
is never inspected or migrated.

References:
- claude-code utils/env.ts getGlobalClaudeFile
- claude-code utils/secureStorage/plainTextStorage.ts getStoragePath
- XDG Base Directory Specification: https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from pathlib import Path

from claude_swap.exceptions import ConfigError

LEGACY_BACKUP_DIRNAME = ".claude-swap-backup"


def get_claude_config_home() -> Path:
    """Return the Claude config home directory (CLAUDE_CONFIG_DIR or ~/.claude)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude"


def get_global_config_path() -> Path:
    """Return the path to the global Claude config file.

    Returns the legacy ``<config_home>/.config.json`` if it exists, else
    ``(CLAUDE_CONFIG_DIR || $HOME)/.claude.json``.
    """
    legacy = get_claude_config_home() / ".config.json"
    if legacy.exists():
        return legacy
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(env) if env else Path.home()
    return base / ".claude.json"


def get_default_claude_config_home() -> Path:
    """Return the *default* profile's config home, ignoring ``CLAUDE_CONFIG_DIR``.

    ``_read_capture_credentials`` has to tell an env var that names the default
    profile from one that names another, since only the former's credential is
    the active store's.
    """
    return Path.home() / ".claude"


def get_default_global_config_path() -> Path:
    """Return the global config path of the *default* profile.

    Same legacy fallback as :func:`get_global_config_path`, but deliberately
    ignores ``CLAUDE_CONFIG_DIR``: callers that mirror the user's real profile
    (session sharing) must not source from another session when invoked from
    inside one.
    """
    legacy = get_default_claude_config_home() / ".config.json"
    if legacy.exists():
        return legacy
    return Path.home() / ".claude.json"


def get_credentials_path() -> Path:
    """Return the path to the Claude credentials file."""
    return get_claude_config_home() / ".credentials.json"


def get_legacy_backup_root() -> Path:
    """Return the upstream backup path for diagnostics only.

    It belongs to an independently installed upstream ``cswap``. Bundled code
    must never inspect, migrate, or delete it.
    """
    return Path.home() / LEGACY_BACKUP_DIRNAME


def get_backup_root() -> Path:
    """Return this bundled engine's private data root.

    The path is separate from both the ccs profile directories and the
    upstream ``~/.claude-swap-backup`` account store. The engine never adopts
    data from either location automatically.
    """
    return Path.home() / ".config" / "cc-swaper" / "account-engine"


_ENGINE_CHILD_DIRECTORIES = ("configs", "credentials", "cache", "sessions")


def _no_dangerous_extended_acl(path: Path) -> bool:
    """Reject ACL allow entries that add read/write/delete access on macOS.

    Deny entries such as the standard ``everyone deny delete`` are safe. ACL
    inspection failures fail closed; ENOENT means the object has no extended
    ACL. The masks mirror the native Swift menu-bar client's trust check.
    """
    if sys.platform != "darwin":
        return True
    acl = ctypes.CDLL(None, use_errno=True)
    acl_get_file = acl.acl_get_file
    acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_int]
    acl_get_file.restype = ctypes.c_void_p
    ctypes.set_errno(0)
    handle = acl_get_file(os.fsencode(path), 0x00000100)  # ACL_TYPE_EXTENDED
    if not handle:
        if ctypes.get_errno() == errno.ENOENT:
            return True
        raise ConfigError(f"Could not inspect extended ACL on {path}")

    try:
        acl_get_entry = acl.acl_get_entry
        acl_get_entry.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
        ]
        acl_get_entry.restype = ctypes.c_int
        acl_get_tag_type = acl.acl_get_tag_type
        acl_get_tag_type.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        acl_get_tag_type.restype = ctypes.c_int
        acl_get_permset_mask = acl.acl_get_permset_mask_np
        acl_get_permset_mask.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)
        ]
        acl_get_permset_mask.restype = ctypes.c_int
        writable_or_readable = sum(1 << bit for bit in range(1, 14))
        entry_id = 0  # ACL_FIRST_ENTRY
        while True:
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = acl_get_entry(handle, entry_id, ctypes.byref(entry))
            if result != 0:
                if ctypes.get_errno() == errno.EINVAL:
                    return True
                raise ConfigError(f"Could not inspect extended ACL entries on {path}")
            if not entry:
                raise ConfigError(f"Could not inspect extended ACL entries on {path}")
            tag = ctypes.c_int()
            if acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
                raise ConfigError(f"Could not inspect extended ACL tags on {path}")
            if tag.value == 1:  # ACL_EXTENDED_ALLOW
                permissions = ctypes.c_uint64()
                if acl_get_permset_mask(entry, ctypes.byref(permissions)) != 0:
                    raise ConfigError(f"Could not inspect extended ACL rights on {path}")
                if permissions.value & writable_or_readable:
                    return False
            elif tag.value != 2:  # ACL_EXTENDED_DENY
                raise ConfigError(f"Unsupported extended ACL entry on {path}")
            entry_id = -1  # ACL_NEXT_ENTRY
    finally:
        acl.acl_free.argtypes = [ctypes.c_void_p]
        acl.acl_free.restype = ctypes.c_int
        acl.acl_free(handle)


def validate_backup_layout(
    root: Path | None = None,
    *,
    create: bool = False,
    create_children: tuple[str, ...] = (),
) -> Path:
    """Validate the account-engine directory tree without following symlinks.

    ``create=False`` is safe to call before constructing loggers or stores: it
    only inspects existing entries and refuses unsafe ownership, type, mode,
    or symlink state. ``create=True`` creates the private base and requested
    direct children, tightening owned directories to 0700 only after an
    ``lstat`` proves each path is a real directory owned by this user.
    """
    root = Path(root or get_backup_root()).absolute()
    expected = (Path.home() / ".config" / "cc-swaper" / "account-engine").absolute()
    if root != expected:
        raise ConfigError(f"Unexpected account-engine storage path: {root}")
    unknown_children = set(create_children) - set(_ENGINE_CHILD_DIRECTORIES)
    if unknown_children:
        raise ValueError(f"unknown account-engine directories: {sorted(unknown_children)}")

    home = Path.home().absolute()
    uid = os.getuid() if hasattr(os, "getuid") else None

    def check_directory(path: Path, *, private: bool, make: bool) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not make:
                return False
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
            try:
                info = path.lstat()
            except OSError as exc:
                raise ConfigError(f"Could not validate account-engine directory {path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"Could not inspect account-engine directory {path}: {exc}") from exc

        if stat.S_ISLNK(info.st_mode):
            raise ConfigError(f"Unsafe account-engine path: refusing symlink {path}")
        if not stat.S_ISDIR(info.st_mode):
            raise ConfigError(f"Unsafe account-engine path: expected a directory at {path}")
        if uid is not None and info.st_uid != uid:
            raise ConfigError(
                f"Unsafe account-engine path: {path} is owned by uid {info.st_uid}, "
                f"expected uid {uid}"
            )
        mode = stat.S_IMODE(info.st_mode)
        if private:
            if mode != 0o700:
                if not create:
                    raise ConfigError(
                        f"Unsafe account-engine permissions on {path}: "
                        f"expected 0700, found {mode:04o}"
                    )
                try:
                    os.chmod(path, 0o700, follow_symlinks=False)
                except (NotImplementedError, OSError) as exc:
                    raise ConfigError(
                        f"Could not secure account-engine directory {path}: {exc}"
                    ) from exc
                try:
                    info = path.lstat()
                except OSError as exc:
                    raise ConfigError(
                        f"Could not revalidate account-engine directory {path}: {exc}"
                    ) from exc
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise ConfigError(f"Unsafe account-engine path changed during validation: {path}")
                if uid is not None and info.st_uid != uid:
                    raise ConfigError(f"Unsafe account-engine path owner changed: {path}")
        else:
            if mode & 0o022:
                raise ConfigError(
                    f"Unsafe writable ancestor for account-engine storage: {path}"
                )
        if not _no_dangerous_extended_acl(path):
            raise ConfigError(f"Unsafe extended ACL grants access to account-engine path: {path}")
        return True

    # The home and ordinary ~/.config ancestor may be readable by other users,
    # but must not be replaceable or writable by them.
    check_directory(home, private=False, make=False)
    config_dir = home / ".config"
    if not check_directory(config_dir, private=False, make=create):
        return root

    # The app-owned parent and engine directories contain account metadata and
    # credentials, so they must remain owner-only.
    for directory in (root.parent, root):
        if not check_directory(directory, private=True, make=create):
            return root

    for child_name in _ENGINE_CHILD_DIRECTORIES:
        child = root / child_name
        if not check_directory(
            child,
            private=True,
            make=create and child_name in create_children,
        ):
            continue
    return root


def validate_engine_path(path: Path) -> None:
    """Refuse symlinked paths inside the private account-engine tree."""
    root = get_backup_root().absolute()
    candidate = Path(path).absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return

    validate_backup_layout(root)
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ConfigError(f"Could not inspect account-engine path {current}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ConfigError(f"Unsafe account-engine path: {current} must be a real directory")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ConfigError(f"Unsafe account-engine path owner: {current}")
        mode = stat.S_IMODE(info.st_mode)
        if mode != 0o700:
            raise ConfigError(
                f"Unsafe account-engine permissions on {current}: expected 0700, found {mode:04o}"
            )

    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ConfigError(f"Could not inspect account-engine file {candidate}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ConfigError(f"Unsafe account-engine path: refusing symlink {candidate}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ConfigError(f"Unsafe account-engine path owner: {candidate}")
    if not _no_dangerous_extended_acl(candidate):
        raise ConfigError(f"Unsafe extended ACL grants access to account-engine path: {candidate}")


def validate_account_lock_path(path: Path) -> None:
    """Validate the stable lock inode kept beside the purgeable engine root."""
    root = get_backup_root().absolute()
    expected = root.parent / ".account-engine.lock"
    candidate = Path(path).absolute()
    if candidate != expected:
        raise ConfigError(f"Unexpected account-engine lock path: {candidate}")
    validate_backup_layout(root)
    try:
        info = candidate.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ConfigError(f"Could not inspect account-engine lock {candidate}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"Unsafe account-engine lock: expected a regular file at {candidate}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ConfigError(f"Unsafe account-engine lock owner: {candidate}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ConfigError(
            f"Unsafe account-engine lock permissions on {candidate}: expected 0600"
        )
    if not _no_dangerous_extended_acl(candidate):
        raise ConfigError(f"Unsafe extended ACL grants access to account-engine lock: {candidate}")


def migration_flag_for(target: Path) -> Path:
    """The interrupted-migration flag for ``target``: a SIBLING of the
    backup root, not a child.

    Spell it here only -- this flag is what turns the collision refusal
    below into an rmtree of the destination, and a second copy drifts.
    """
    return target.parent / f".{target.name}.migrating"


def migrate_legacy_backup_dir(target: Path) -> bool:
    """Compatibility no-op; bundled builds never inspect upstream account data."""
    return False
