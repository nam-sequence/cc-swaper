"""Build private launch overlays for MCP servers and plugin seeding.

Managed profiles use their own Claude configuration directory. This module
copies only the main configuration's top-level ``mcpServers`` map into a
private, immutable ``--mcp-config`` snapshot. OAuth state, project metadata,
trust state, and account plugin caches remain in their original locations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .profiles import Profile, ProfileStore


_SNAPSHOT_VERSION = 1
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_MAX_CONFIG_BYTES = 8 * 1024 * 1024
_PLUGIN_SEED_ENV = "CLAUDE_CODE_PLUGIN_SEED_DIR"
_SENSITIVE_NAME_PARTS = (
    "TOKEN", "SECRET", "PASSWORD", "PASSWD", "AUTH", "KEY", "CREDENTIAL", "ASKPASS"
)
_CREDENTIAL_QUERY_PARTS = _SENSITIVE_NAME_PARTS + ("SIGNATURE",)
_CREDENTIAL_OPTION_PARTS = _SENSITIVE_NAME_PARTS + (
    "HEADER", "COOKIE", "BEARER", "SIGNATURE"
)
_COMMAND_FLAG = re.compile(r"(?<![A-Za-z0-9_-])--([A-Za-z0-9][A-Za-z0-9_-]*)(?=$|[\s=])")
_COMMAND_ASSIGNMENT = re.compile(
    r"(?:^|[\s\"';,])([A-Za-z_][A-Za-z0-9_.-]*)\s*="
)


def prepare_shared_mcp_plugins(
    store: ProfileStore, profile: Profile, cwd: Path
) -> tuple[list[str], dict[str, str]]:
    """Return launch arguments and environment overlays for a managed profile.

    The default profile already uses Claude's main configuration and needs no
    overlay. Managed profiles receive eligible main-account MCP servers while
    keeping profile and project definitions authoritative when names overlap.
    """

    if profile.config_dir is None:
        return [], {}

    _validate_managed_profile(store, profile)

    main_home = Path.home()
    resolved_cwd = _resolved_cwd(cwd)
    project_root = _project_root(resolved_cwd)
    shared_servers = _mcp_servers(main_home / ".claude.json")
    profile_config = _read_json_object(profile.config_dir / ".claude.json")
    profile_servers = _mcp_servers_from_payload(profile_config, ".claude.json")
    profile_project_servers = _profile_project_mcp_servers(
        profile_config, (resolved_cwd, project_root)
    )
    project_servers = _mcp_servers(project_root / ".mcp.json")

    collisions = (
        profile_servers.keys()
        | profile_project_servers.keys()
        | project_servers.keys()
    )
    servers: dict[str, Any] = {}
    for name, definition in shared_servers.items():
        if name in collisions or _has_embedded_credential(definition):
            continue
        if not isinstance(definition, dict):
            raise ValueError("invalid MCP server definition in .claude.json")
        servers[name] = definition

    args: list[str] = []
    if servers:
        assert profile.config_dir is not None
        snapshot = _write_mcp_snapshot(profile.config_dir, servers)
        args = ["--mcp-config", str(snapshot)]

    env: dict[str, str] = {}
    plugin_seed = _validated_plugin_seed(main_home)
    if plugin_seed is not None:
        env[_PLUGIN_SEED_ENV] = str(plugin_seed)

    return args, env


def _validate_managed_profile(store: ProfileStore, profile: Profile) -> None:
    name = profile.name
    if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name):
        raise ValueError("invalid managed profile name")

    expected = store.profiles_dir / name
    config_dir = Path(profile.config_dir).absolute()
    if config_dir != expected:
        raise ValueError("managed profile directory does not match its profile store")

    _validate_private_directory(store.home)
    _validate_private_directory(store.profiles_dir)
    _validate_private_directory(config_dir)


def _validate_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise ValueError("unsafe private profile directory") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("unsafe private profile directory")


def _resolved_cwd(cwd: Path) -> Path:
    if not isinstance(cwd, Path):
        cwd = Path(cwd)
    return cwd.expanduser().resolve(strict=False)


def _project_root(cwd: Path) -> Path:
    directory = _resolved_cwd(cwd)
    while True:
        marker = directory / ".git"
        try:
            info = marker.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise ValueError("cannot safely inspect project Git marker") from None
        else:
            if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                return directory
        if directory == directory.parent:
            break
        directory = directory.parent
    return _resolved_cwd(cwd)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """Read a regular, user-owned JSON file without following a final symlink."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(f"cannot safely inspect {path.name}") from None

    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_size > _MAX_CONFIG_BYTES
        ):
            raise ValueError(f"cannot safely inspect {path.name}")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            contents = handle.read(_MAX_CONFIG_BYTES + 1)
        if len(contents) > _MAX_CONFIG_BYTES:
            raise ValueError(f"cannot safely inspect {path.name}")
    finally:
        if fd >= 0:
            os.close(fd)

    try:
        payload = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError(f"invalid JSON in {path.name}") from None
    if not isinstance(payload, dict):
        raise ValueError(f"invalid JSON object in {path.name}")
    return payload


def _mcp_servers(path: Path) -> dict[str, Any]:
    payload = _read_json_object(path)
    return _mcp_servers_from_payload(payload, path.name)


def _mcp_servers_from_payload(
    payload: dict[str, Any] | None, source_name: str
) -> dict[str, Any]:
    if payload is None:
        return {}
    servers = payload.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"invalid MCP server map in {source_name}")
    return servers


def _profile_project_mcp_servers(
    payload: dict[str, Any] | None, project_paths: tuple[Path, ...]
) -> dict[str, Any]:
    if payload is None:
        return {}
    projects = payload.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("invalid project map in .claude.json")

    servers: dict[str, Any] = {}
    for project_path in dict.fromkeys(project_paths):
        project = projects.get(str(project_path))
        if project is None:
            continue
        if not isinstance(project, dict):
            raise ValueError("invalid project configuration in .claude.json")
        local_servers = project.get("mcpServers", {})
        if not isinstance(local_servers, dict):
            raise ValueError("invalid project MCP server map in .claude.json")
        servers.update(local_servers)
    return servers


def _uses_headers_helper(definition: Any) -> bool:
    return isinstance(definition, dict) and "headersHelper" in definition


def _has_embedded_credential(definition: Any) -> bool:
    """Identify credential-bearing definitions unsafe to copy across profiles."""

    if not isinstance(definition, dict):
        return False
    if _uses_headers_helper(definition):
        return True

    headers = definition.get("headers")
    if headers:
        return True

    if "env" in definition:
        environment = definition["env"]
        if not isinstance(environment, dict):
            return True
        if environment:
            if any(_has_sensitive_name(key) for key in environment):
                return True
            if (
                set(environment) != {"FIRECRAWL_API_URL"}
                or not _is_safe_env_url(environment["FIRECRAWL_API_URL"])
            ):
                return True

    url = definition.get("url")
    if url is not None and _url_has_credentials(url):
        return True
    if _command_args_have_credentials(definition):
        return True
    return False


def _has_sensitive_name(name: Any) -> bool:
    if not isinstance(name, str):
        return True
    upper_name = name.upper()
    return any(part in upper_name for part in _SENSITIVE_NAME_PARTS)


def _is_safe_env_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and parsed.netloc
        and hostname
        and not parsed.fragment
        and not _url_has_credentials(value)
    )


def _command_args_have_credentials(definition: dict[str, Any]) -> bool:
    command = definition.get("command")
    args = definition.get("args", [])
    if command is not None and not isinstance(command, str):
        return True
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        return True

    values = ([command] if isinstance(command, str) else []) + args
    for value in values:
        if value in {"-H", "-u"}:
            return True
        if re.search(r"\b(?:authorization|proxy-authorization|x-api-key)\s*:", value, re.IGNORECASE):
            return True
        if "://" in value and _url_has_credentials(value):
            return True
        if any(
            _has_credential_option(match.group(1))
            for match in _COMMAND_FLAG.finditer(value)
        ):
            return True
        if any(
            _has_sensitive_name(match.group(1))
            for match in _COMMAND_ASSIGNMENT.finditer(value)
        ):
            return True
        if re.search(r"\b(?:bearer|basic)\s+\S+", value, re.IGNORECASE):
            return True
    return False


def _has_credential_option(name: str) -> bool:
    upper_name = name.upper()
    return any(part in upper_name for part in _CREDENTIAL_OPTION_PARTS)


def _url_has_credentials(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    if "@" in parsed.netloc:
        return True
    for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        upper_key = key.upper()
        if any(part in upper_key for part in _CREDENTIAL_QUERY_PARTS):
            return True
        if re.search(r"\b(?:bearer|basic)\s+\S+", query_value, re.IGNORECASE):
            return True
    return False


def _write_mcp_snapshot(
    profile_config_dir: Path, servers: dict[str, Any]
) -> Path:
    generated_dir = _ensure_child_directory(profile_config_dir, ".ccs-generated")
    mcp_dir = _ensure_child_directory(generated_dir, "mcp")

    encoded = (
        json.dumps(
            {"mcpServers": servers},
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    digest = hashlib.sha256(encoded).hexdigest()
    destination = mcp_dir / f"v{_SNAPSHOT_VERSION}-{digest}.json"

    if destination.exists() or destination.is_symlink():
        _verify_snapshot(destination, encoded)
        return destination

    temporary_path: Path | None = None
    temporary_fd: int | None = None
    try:
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=".mcp-snapshot-", suffix=".tmp", dir=mcp_dir
        )
        temporary_path = Path(temporary_name)
        os.fchmod(temporary_fd, 0o600)
        with os.fdopen(temporary_fd, "wb") as handle:
            temporary_fd = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            os.link(temporary_path, destination, follow_symlinks=False)
        except FileExistsError:
            _verify_snapshot(destination, encoded)
        finally:
            temporary_path.unlink()
            temporary_path = None
        _fsync_directory(mcp_dir)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    _verify_snapshot(destination, encoded)
    return destination


def _ensure_child_directory(parent: Path, name: str) -> Path:
    path = parent / name
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _validate_private_directory(path)
    return path


def _verify_snapshot(path: Path, expected: bytes) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise ValueError("unsafe generated MCP snapshot") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size != len(expected)
        ):
            raise ValueError("unsafe generated MCP snapshot")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            actual = handle.read(len(expected) + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if actual != expected:
        raise ValueError("generated MCP snapshot content does not match its address")


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validated_plugin_seed(home: Path) -> Path | None:
    claude_dir = home / ".claude"
    plugins_dir = claude_dir / "plugins"
    try:
        _validate_owned_directory(claude_dir)
        _validate_owned_directory(plugins_dir)
        _validate_owned_file(plugins_dir / "known_marketplaces.json")
        _validate_owned_directory(plugins_dir / "marketplaces")
        _validate_owned_directory(plugins_dir / "cache")
    except (OSError, ValueError):
        return None
    return plugins_dir


def _validate_owned_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError("unsafe Claude plugin directory")


def _validate_owned_file(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ValueError("unsafe Claude plugin metadata file")
