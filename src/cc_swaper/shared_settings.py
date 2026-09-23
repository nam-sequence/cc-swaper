"""Prepare a managed profile's shared Claude settings and resources.

Managed profiles have their own ``CLAUDE_CONFIG_DIR``. This module creates a
private, versioned overlay for the non-secret parts of the user's shared
Claude settings and a stable resource view for ``claude --add-dir``. Generated
files are content-addressed and never replaced, so background Claude sessions
can continue using them after the parent ``ccs`` process exits.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .profiles import Profile, ProfileStore


_MAX_SETTINGS_BYTES = 2 * 1024 * 1024
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_SHARED_SETTINGS_KEYS = frozenset(
    {
        "agentPushNotifEnabled",
        "askUserQuestionTimeout",
        "autoCompactEnabled",
        "autoCompactWindow",
        "effortLevel",
        "enabledPlugins",
        "extraKnownMarketplaces",
        "inputNeededNotifEnabled",
        "modelSettings",
        "preferredNotifChannel",
        "skillOverrides",
        "skipWorkflowUsageWarning",
        "statusLine",
        "teammateMode",
        "theme",
        "tui",
        "verbose",
        "model",
        "fallbackModel",
        "outputStyle",
        "env",
    }
)
_SHARED_ENV_KEYS = frozenset(
    {
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    }
)
_SENSITIVE_ENV_EXACT = frozenset(
    {
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
        "CLAUDE_CONFIG_DIR",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NODE_TLS_REJECT_UNAUTHORIZED",
    }
)
_CREDENTIAL_QUERY_KEY_PARTS = (
    "token",
    "password",
    "passwd",
    "secret",
    "credential",
    "signature",
    "authorization",
    "authentication",
    "apikey",
    "accesskey",
    "privatekey",
    "clientsecret",
    "bearer",
)
_CREDENTIAL_VALUE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^bearer\s+\S+",
        r"^(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})$",
        r"^glpat-[A-Za-z0-9_-]{20,}$",
        r"^xox[baprs]-[A-Za-z0-9-]{10,}$",
        r"^AKIA[0-9A-Z]{16}$",
        r"^eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$",
        r"^sk-[A-Za-z0-9_-]{20,}$",
        r"^(?:sk|rk)_(?:live|prod)_[A-Za-z0-9]{16,}$",
        r"^AIza[0-9A-Za-z_-]{30,}$",
        r"^npm_[A-Za-z0-9]{30,}$",
    )
)
_HEX_DIGEST = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$", re.IGNORECASE)


def prepare_shared_settings(
    store: ProfileStore, profile: Profile
) -> tuple[list[str], dict[str, str]]:
    """Return Claude arguments and environment overrides for a profile.

    The default profile intentionally uses Claude's ordinary ``~/.claude``
    configuration and therefore needs no overlay. Managed profiles inherit
    only shared settings that their own ``settings.json`` does not define,
    with parent-shell authentication and provider routing values removed.
    """

    if profile.config_dir is None:
        return [], {}

    _validate_managed_profile(store, profile)
    main_claude = Path.home() / ".claude"
    if main_claude.is_symlink():
        raise ValueError("refusing symlink for the shared Claude directory")
    if main_claude.exists() and not main_claude.is_dir():
        raise ValueError("shared Claude path is not a directory")

    main_settings_path = main_claude / "settings.json"
    profile_settings_path = profile.config_dir / "settings.json"
    main_settings = _read_settings(main_settings_path, "shared")
    managed_settings = _read_settings(profile_settings_path, "managed")
    inherited = _filter_shared_settings(main_settings, managed_settings)

    generated_profile = _profile_generated_root(profile)
    args: list[str] = []
    if inherited:
        settings_path = _write_settings_snapshot(generated_profile, inherited)
        args.extend(["--settings", str(settings_path)])

    resource_path = _create_resource_view(generated_profile, main_claude)
    if resource_path is None:
        return args, {}
    args.extend(["--add-dir", str(resource_path)])
    return args, {"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1"}


def _validate_managed_profile(store: ProfileStore, profile: Profile) -> None:
    if not _PROFILE_NAME.fullmatch(profile.name):
        raise ValueError("invalid managed profile name")

    expected = Path(store.profiles_dir) / profile.name
    if Path(profile.config_dir).absolute() != expected.absolute():
        raise ValueError("managed profile directory is outside the profile store")
    _validate_private_directory(Path(profile.config_dir), "managed profile directory")


def _validate_private_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"unsafe {label}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ValueError(f"unsafe {label}")


def _ensure_private_directory(path: Path) -> None:
    """Create one private child directory without following symlinks."""

    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError("unsafe generated directory") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("unsafe generated directory")
    if created:
        os.chmod(path, 0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("generated directory is not private")


def _profile_generated_root(profile: Profile) -> Path:
    assert profile.config_dir is not None
    generated = Path(profile.config_dir) / ".ccs-generated"
    profile_root = generated / "settings-and-resources"
    _ensure_private_directory(generated)
    _ensure_private_directory(profile_root)
    return profile_root


def _read_settings(path: Path, label: str) -> dict[str, Any]:
    """Read a bounded, strict JSON object without following a symlink."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"unsafe {label} settings file") from exc

    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ValueError(f"unsafe {label} settings file")
        if info.st_size > _MAX_SETTINGS_BYTES:
            raise ValueError(f"{label} settings file exceeds 2 MiB")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, _MAX_SETTINGS_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_SETTINGS_BYTES:
                raise ValueError(f"{label} settings file exceeds 2 MiB")
    finally:
        os.close(fd)

    try:
        text = b"".join(chunks).decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError(f"invalid {label} settings JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} settings JSON must be an object")
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _filter_shared_settings(
    shared: dict[str, Any], managed: dict[str, Any]
) -> dict[str, Any]:
    inherited: dict[str, Any] = {}
    for key, value in shared.items():
        if key not in _SHARED_SETTINGS_KEYS or key in managed:
            continue
        if key == "env":
            if not isinstance(value, dict):
                raise ValueError("shared settings env must be an object")
            safe_env: dict[str, str] = {}
            for env_name, env_value in value.items():
                if not isinstance(env_name, str) or not isinstance(env_value, str):
                    raise ValueError("shared settings env entries must be strings")
                if env_name in _SHARED_ENV_KEYS and not _is_sensitive_env_name(env_name):
                    safe_env[env_name] = env_value
            if safe_env:
                inherited[key] = safe_env
        elif key == "extraKnownMarketplaces":
            if not isinstance(value, dict):
                continue
            safe_marketplaces = {
                name: entry
                for name, entry in value.items()
                if not _contains_credential_source_url(entry)
            }
            if safe_marketplaces:
                inherited[key] = safe_marketplaces
        else:
            inherited[key] = value
    return inherited


def _contains_credential_source_url(value: Any, *, in_source: bool = False) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            child_in_source = in_source or key.casefold() == "source"
            if child_in_source and isinstance(child, str):
                if _url_contains_credentials(child):
                    return True
            elif _contains_credential_source_url(child, in_source=child_in_source):
                return True
        return False
    if isinstance(value, list):
        return any(
            _contains_credential_source_url(child, in_source=in_source)
            for child in value
        )
    if in_source and isinstance(value, str):
        return _url_contains_credentials(value)
    return False


def _url_contains_credentials(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        # Malformed URL-like values are not safe to pass through.
        return "://" in value or value.startswith("git@")

    if (
        "@" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or re.match(r"^[^/@\s]+@[^:/\s]+:", value) is not None
    ):
        return True

    for query_key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = re.sub(r"[^a-z0-9]", "", query_key.casefold())
        if (
            any(part in normalized_key for part in _CREDENTIAL_QUERY_KEY_PARTS)
            or normalized_key in {"auth", "oauth", "sig"}
            or normalized_key.endswith("auth")
        ):
            return True
        if _credential_like_query_value(normalized_key, query_value):
            return True
    return False


def _credential_like_query_value(normalized_key: str, value: str) -> bool:
    for pattern in _CREDENTIAL_VALUE_PATTERNS:
        if pattern.fullmatch(value):
            return True
    if (
        normalized_key in {"sha", "commit", "commitsha", "checksum", "digest", "hash"}
        and _HEX_DIGEST.fullmatch(value)
    ):
        return False
    if len(value) < 32:
        return False
    counts: dict[str, int] = {}
    for character in value:
        counts[character] = counts.get(character, 0) + 1
    # A long opaque value is credential-like even when the query key is vague.
    # The prefix-based checks above cover common tokens at shorter lengths.
    return len(value) >= 40 and len(counts) >= 18 and _has_mixed_token_classes(value)


def _has_mixed_token_classes(value: str) -> bool:
    classes = sum(
        (
            any(character.islower() for character in value),
            any(character.isupper() for character in value),
            any(character.isdigit() for character in value),
            any(not character.isalnum() for character in value),
        )
    )
    return classes >= 2


def _is_sensitive_env_name(name: str) -> bool:
    return (
        name.startswith("ANTHROPIC_")
        or name.startswith("CLAUDE_CODE_OAUTH_")
        or name.startswith("CLAUDE_CODE_USE_")
        or name.startswith("CLAUDE_CODE_CLIENT_")
        or (name.startswith("CLAUDE_CODE_SKIP_") and name.endswith("_AUTH"))
        or name in _SENSITIVE_ENV_EXACT
    )


def _write_settings_snapshot(generated_profile: Path, payload: dict[str, Any]) -> Path:
    directory = generated_profile / "settings"
    _ensure_private_directory(directory)
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    destination = directory / f"{digest}.json"
    temporary_fd = -1
    temporary_path: Path | None = None
    try:
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".tmp", dir=directory
        )
        temporary_path = Path(temporary_name)
        os.fchmod(temporary_fd, 0o600)
        with os.fdopen(temporary_fd, "wb") as handle:
            temporary_fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, destination, follow_symlinks=False)
        except FileExistsError:
            _verify_existing_snapshot(destination, content)
        else:
            try:
                directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            except OSError:
                pass
            else:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        return destination
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _verify_existing_snapshot(path: Path, expected: bytes) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError("unsafe existing settings snapshot") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("unsafe existing settings snapshot")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    if b"".join(chunks) != expected:
        raise ValueError("existing settings snapshot does not match its content hash")


def _create_resource_view(generated_profile: Path, main_claude: Path) -> Path | None:
    resources = generated_profile / "resources"
    _ensure_private_directory(resources)

    temporary = Path(tempfile.mkdtemp(prefix=".view-", dir=resources))
    os.chmod(temporary, 0o700)
    try:
        _copy_shared_resources(main_claude, temporary)
        if not _has_regular_file(temporary):
            _remove_tree(temporary)
            temporary = Path()
            return None
        digest = _digest_tree(temporary)
        destination = resources / digest
        with _resource_publish_lock(resources):
            candidates = [destination, *sorted(resources.glob(f"{digest}-*"))]
            for candidate in candidates:
                if not candidate.exists() and not candidate.is_symlink():
                    continue
                if candidate.is_symlink() or not candidate.is_dir():
                    raise ValueError("unsafe existing resource view")
                if _digest_tree(candidate) == digest:
                    _remove_tree(temporary)
                    temporary = Path()
                    return candidate
                # A running Claude session can edit its own copied view. Keep
                # that view for the session and publish a fresh copy under a
                # different name rather than blocking future launches.
            if destination.exists() or destination.is_symlink():
                while True:
                    candidate = resources / f"{digest}-{uuid.uuid4().hex[:12]}"
                    if not candidate.exists() and not candidate.is_symlink():
                        destination = candidate
                        break
            os.rename(temporary, destination)
            temporary = Path()
        return destination
    finally:
        if temporary != Path() and temporary.exists():
            _remove_tree(temporary)


def _has_regular_file(root: Path) -> bool:
    try:
        entries = sorted(os.scandir(root), key=lambda item: item.name)
    except OSError as exc:
        raise ValueError("unsafe generated resource view") from exc
    for entry in entries:
        info = entry.stat(follow_symlinks=False)
        _validate_generated_info(info)
        if stat.S_ISREG(info.st_mode):
            return True
        if stat.S_ISDIR(info.st_mode) and _has_regular_file(root / entry.name):
            return True
    return False


@contextmanager
def _resource_publish_lock(resources: Path):
    lock_path = resources / ".publish.lock"
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        fd = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        try:
            fd = os.open(lock_path, flags)
        except OSError as exc:
            raise ValueError("unsafe resource view lock") from exc
    except OSError as exc:
        raise ValueError("unsafe resource view lock") from exc
    try:
        if created:
            os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("unsafe resource view lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _copy_shared_resources(source_root: Path, destination_root: Path) -> None:
    try:
        root_info = source_root.lstat()
    except FileNotFoundError:
        return
    _validate_source_info(root_info, "shared Claude directory")
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("unsafe shared Claude directory")

    claude_md = source_root / "CLAUDE.md"
    _copy_optional_file(claude_md, destination_root / "CLAUDE.md")

    for name in ("rules", "skills", "commands", "agents"):
        source = source_root / name
        target = destination_root / ".claude" / name
        _copy_optional_directory(source, target, skills=(name == "skills"))


def _copy_optional_file(source: Path, destination: Path) -> None:
    try:
        info = source.lstat()
    except FileNotFoundError:
        return
    _validate_source_info(info, "shared Claude resource")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("shared Claude resource is not a regular file")
    _ensure_staging_parent(destination.parent)
    _copy_regular_file(source, destination)


def _copy_optional_directory(
    source: Path, destination: Path, *, skills: bool = False
) -> None:
    try:
        info = source.lstat()
    except FileNotFoundError:
        return
    _validate_source_info(info, "shared Claude resource directory")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("shared Claude resource is not a directory")
    _copy_directory(source, destination, skip_skill_files=skills)


def _copy_directory(source: Path, destination: Path, *, skip_skill_files: bool = False) -> None:
    _ensure_staging_parent(destination.parent)
    destination.mkdir(mode=0o700)
    os.chmod(destination, 0o700)
    try:
        entries = sorted(os.scandir(source), key=lambda item: item.name)
        for entry in entries:
            name = entry.name
            if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
                raise ValueError("unsafe path in shared Claude resources")
            if skip_skill_files and name == "synced":
                continue
            item_info = entry.stat(follow_symlinks=False)
            child_source = source / name
            child_destination = destination / name
            if stat.S_ISLNK(item_info.st_mode):
                if not skip_skill_files:
                    raise ValueError("unsafe symlink in shared Claude resources")
                _copy_linked_skill(child_source, child_destination)
            elif stat.S_ISDIR(item_info.st_mode):
                _validate_source_info(item_info, "shared Claude resource")
                _copy_directory(child_source, child_destination)
            elif stat.S_ISREG(item_info.st_mode):
                _validate_source_info(item_info, "shared Claude resource")
                if skip_skill_files:
                    # Skills are directories with an individual SKILL.md. Loose
                    # files in the global skills root are not authored skills.
                    continue
                _copy_regular_file(child_source, child_destination)
            else:
                raise ValueError("unsupported file in shared Claude resources")
    except OSError as exc:
        raise ValueError("could not safely read shared Claude resources") from exc


def _validate_source_info(info: os.stat_result, label: str) -> None:
    if (
        stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError(f"unsafe {label}")


def _copy_linked_skill(source_link: Path, destination: Path) -> bool:
    """Snapshot one authored skill link after validating its resolved target."""

    home = Path.home().resolve(strict=True)
    try:
        target = source_link.resolve(strict=False)
        target.relative_to(home)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("unsafe authored skill symlink target") from exc

    try:
        target_info = target.lstat()
    except FileNotFoundError:
        # A main-account skill may have been removed from its source checkout.
        # An in-home dangling link is simply absent from the shared view.
        return False
    except OSError as exc:
        raise ValueError("unsafe authored skill symlink target") from exc

    current = target
    while current != home:
        try:
            info = current.lstat()
            current.relative_to(home)
        except (OSError, ValueError) as exc:
            raise ValueError("unsafe authored skill symlink target") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ValueError("unsafe authored skill symlink target")
        current = current.parent
    home_info = home.lstat()
    if (
        not stat.S_ISDIR(home_info.st_mode)
        or home_info.st_uid != os.getuid()
        or stat.S_IMODE(home_info.st_mode) & 0o022
    ):
        raise ValueError("unsafe authored skill home")

    skill_file = target / "SKILL.md"
    try:
        skill_info = skill_file.lstat()
    except OSError as exc:
        raise ValueError("authored skill symlink target has no safe SKILL.md") from exc
    _validate_source_info(target_info, "authored skill target")
    _validate_source_info(skill_info, "authored skill entrypoint")
    if not stat.S_ISDIR(target_info.st_mode) or not stat.S_ISREG(skill_info.st_mode):
        raise ValueError("authored skill symlink target has no safe SKILL.md")
    synced = Path.home() / ".claude" / "skills" / "synced"
    try:
        synced_target = synced.resolve(strict=False)
        target.relative_to(synced_target)
    except ValueError:
        pass
    except (OSError, RuntimeError) as exc:
        raise ValueError("unsafe synced-skill path") from exc
    else:
        raise ValueError("authored skill symlink resolves into excluded synced skills")
    _copy_directory(target, destination)
    return True


def _ensure_staging_parent(path: Path) -> None:
    if path.exists():
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("unsafe generated resource path")
        return
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def _copy_regular_file(source: Path, destination: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise ValueError("unsafe shared Claude resource file") from exc
    target_fd = -1
    try:
        info = os.fstat(source_fd)
        _validate_source_info(info, "shared Claude resource file")
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("shared Claude resource is not a regular file")
        target_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(target_fd, 0o600)
        remaining = info.st_size
        while True:
            chunk = os.read(source_fd, 65536)
            if not chunk:
                break
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("shared Claude resource changed while being copied")
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        if remaining != 0:
            raise ValueError("shared Claude resource changed while being copied")
        os.fsync(target_fd)
    except OSError as exc:
        raise ValueError("could not safely copy shared Claude resource") from exc
    finally:
        os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()

    def visit(directory: Path, relative: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise ValueError("unsafe generated resource view") from exc
        for entry in entries:
            child_name = entry.name
            child_relative = relative / child_name
            info = entry.stat(follow_symlinks=False)
            _validate_generated_info(info)
            encoded_path = child_relative.as_posix().encode("utf-8")
            if stat.S_ISDIR(info.st_mode):
                digest.update(b"D")
                digest.update(len(encoded_path).to_bytes(4, "big"))
                digest.update(encoded_path)
                visit(directory / child_name, child_relative)
            elif stat.S_ISREG(info.st_mode):
                digest.update(b"F")
                digest.update(len(encoded_path).to_bytes(4, "big"))
                digest.update(encoded_path)
                digest.update(info.st_size.to_bytes(8, "big"))
                file_path = directory / child_name
                fd = os.open(
                    file_path,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    opened = os.fstat(fd)
                    _validate_generated_info(opened)
                    if not stat.S_ISREG(opened.st_mode) or opened.st_size != info.st_size:
                        raise ValueError("generated resource changed while hashing")
                    remaining = opened.st_size
                    while True:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        digest.update(chunk)
                    if remaining != 0:
                        raise ValueError("generated resource changed while hashing")
                finally:
                    os.close(fd)
            else:
                raise ValueError("unsafe generated resource view")

    visit(root, Path())
    return digest.hexdigest()


def _validate_generated_info(info: os.stat_result) -> None:
    expected_mode = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
    if (
        info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != expected_mode
        or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
    ):
        raise ValueError("unsafe generated resource view")


def _remove_tree(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("refusing symlink in generated resource cleanup")
    shutil.rmtree(path)
