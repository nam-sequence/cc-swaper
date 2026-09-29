"""Isolation checks for the engine vendored into the cc-swaper package."""

from __future__ import annotations

import json
import os
import stat
import sys
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, macos_keychain
from claude_swap.credentials import CLAUDE_CODE_KEYCHAIN_SERVICE, SECURITY_SERVICE
from claude_swap.exceptions import ConfigError
from claude_swap.locking import FileLock
from claude_swap.json_output import USAGE_TOKEN_EXPIRED
from claude_swap.models import Platform
from claude_swap.paths import get_backup_root, get_global_config_path, get_legacy_backup_root
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry


def test_backup_store_is_separate_from_ccs_profiles_and_upstream_cswap(temp_home):
    root = get_backup_root()
    assert root == temp_home / ".config" / "cc-swaper" / "account-engine"
    assert root != temp_home / ".config" / "cc-swaper" / "profiles"
    assert root != temp_home / ".claude-swap-backup"


def test_private_account_keychain_service_is_not_upstream_service():
    assert SECURITY_SERVICE == "cc-swaper-engine"
    assert SECURITY_SERVICE not in {"claude-swap", "claude-code"}


def test_stable_account_lock_is_private_outside_the_purge_root(temp_home):
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    lock_path = switcher.backup_dir.parent / ".account-engine.lock"
    assert switcher.lock_file == lock_path
    assert lock_path.parent.stat().st_mode & 0o777 == 0o700

    with FileLock(lock_path):
        pass
    info = lock_path.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) == 0o600

    assert ClaudeAccountSwitcher().lock_file == lock_path
    outside = temp_home / "outside-lock-target"
    outside.write_text("sentinel")
    lock_path.unlink()
    lock_path.symlink_to(outside)
    with pytest.raises(ConfigError, match="Unsafe account-engine lock"):
        ClaudeAccountSwitcher()
    assert outside.read_text() == "sentinel"


def test_purge_keeps_stable_lock_outside_removed_engine_root(temp_home):
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    lock_path = switcher.lock_file

    with patch("builtins.input", return_value="y"):
        switcher.purge()

    assert not switcher.backup_dir.exists()
    info = lock_path.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_size == 0
    assert ClaudeAccountSwitcher().lock_file == lock_path


@pytest.mark.no_keychain_fake
def test_help_and_empty_json_list_do_not_access_keychain(temp_home, monkeypatch):
    calls = []
    upstream_root = get_legacy_backup_root()
    upstream_root.mkdir()
    upstream_marker = upstream_root / "sequence.json"
    upstream_marker.write_text("upstream account data")
    path_exists = Path.exists

    def deny_upstream_path_probe(path: Path) -> bool:
        if path == upstream_root or upstream_root in path.parents:
            raise AssertionError("the external upstream account store was inspected")
        return path_exists(path)

    monkeypatch.setattr(Path, "exists", deny_upstream_path_probe)

    def record(operation):
        def wrapped(*args, **kwargs):
            calls.append((operation, args, kwargs))
            return None

        return wrapped

    with patch.object(macos_keychain, "get_password", side_effect=record("get")), \
         patch.object(macos_keychain, "item_exists", side_effect=record("exists")), \
         patch.object(macos_keychain, "set_password", side_effect=record("set")), \
         patch.object(macos_keychain, "delete_password", side_effect=record("delete")):
        monkeypatch.setattr(sys, "argv", ["ccs-accounts", "--help"])
        with pytest.raises(SystemExit) as help_exit:
            cli.main()
        assert help_exit.value.code == 0

        switcher = ClaudeAccountSwitcher()
        payload = switcher.list_accounts(json_output=True)

    assert payload == {
        "schemaVersion": 1,
        "activeAccountNumber": None,
        "accounts": [],
    }
    assert calls == []
    assert upstream_marker.read_text() == "upstream account data"


@pytest.mark.no_keychain_fake
def test_managed_list_and_status_only_read_active_keychain(temp_home, monkeypatch):
    calls = []
    active_credentials = json.dumps({
        "claudeAiOauth": {
            "accessToken": "access",
            "refreshToken": "refresh",
            "expiresAt": 4_000_000_000_000,
        }
    })

    def get_password(service, account):
        calls.append(("get", service, account))
        if service == CLAUDE_CODE_KEYCHAIN_SERVICE:
            return active_credentials
        return None

    def record(operation):
        def wrapped(service, account, *args):
            calls.append((operation, service, account))
            return None

        return wrapped

    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    switcher._setup_directories()
    switcher._write_json(switcher.sequence_file, {
        "activeAccountNumber": 1,
        "lastUpdated": "2026-01-01T00:00:00Z",
        "sequence": [1],
        "accounts": {
            "1": {
                "email": "active@example.com",
                "uuid": "account-uuid",
                "organizationUuid": "",
                "organizationName": "",
                "added": "2026-01-01T00:00:00Z",
            }
        },
    })
    get_global_config_path().write_text(json.dumps({
        "oauthAccount": {"emailAddress": "active@example.com", "accountUuid": "account-uuid"}
    }))
    monkeypatch.setattr(
        switcher,
        "_collect_usage_entries",
        lambda infos, **kwargs: {str(infos[0][0]): UsageEntry()},
    )

    with patch.object(macos_keychain, "get_password", side_effect=get_password), \
         patch.object(macos_keychain, "set_password", side_effect=record("set")), \
         patch.object(macos_keychain, "delete_password", side_effect=record("delete")):
        listed = switcher.list_accounts(json_output=True)
        status = switcher.status(json_output=True)

    assert listed["activeAccountNumber"] == 1
    assert status["active"]["number"] == 1
    assert calls
    assert all(operation == "get" for operation, *_ in calls)
    assert all(service == CLAUDE_CODE_KEYCHAIN_SERVICE for _, service, *_ in calls)


@pytest.mark.no_keychain_fake
def test_expired_active_usage_does_not_refresh_or_write_shared_credential(temp_home):
    switcher = ClaudeAccountSwitcher()
    expired = json.dumps({
        "claudeAiOauth": {
            "accessToken": "access",
            "refreshToken": "refresh",
            "expiresAt": 1,
        }
    })
    with patch.object(switcher, "_write_credentials", side_effect=AssertionError), \
         patch.object(switcher, "_write_account_credentials", side_effect=AssertionError), \
         patch("claude_swap.oauth.try_refresh_oauth_credentials", side_effect=AssertionError), \
         patch("claude_swap.oauth.try_fetch_usage_for_account", side_effect=AssertionError):
        result = switcher._fetch_active_usage("1", "active@example.com", expired)

    assert result.sentinel == USAGE_TOKEN_EXPIRED


def test_account_store_creation_uses_private_directory_modes(temp_home):
    if sys.platform == "win32":
        pytest.skip("POSIX file modes are not meaningful on Windows")
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    assert switcher.backup_dir.parent.stat().st_mode & 0o777 == 0o700
    assert switcher.backup_dir.stat().st_mode & 0o777 == 0o700
    assert switcher.credentials_dir.stat().st_mode & 0o777 == 0o700


def _make_private_engine_parent(temp_home: Path) -> Path:
    parent = temp_home / ".config" / "cc-swaper"
    parent.mkdir(parents=True, mode=0o700)
    os.chmod(parent, 0o700)
    return parent


def test_symlinked_engine_root_refuses_constructor_and_list_without_touching_target(
    temp_home, monkeypatch, capsys,
):
    parent = _make_private_engine_parent(temp_home)
    external = get_legacy_backup_root()
    external.mkdir(mode=0o750)
    marker = external / "sequence.json"
    marker.write_text("external upstream sentinel", encoding="utf-8")
    mode_before = stat.S_IMODE(external.lstat().st_mode)
    bytes_before = marker.read_bytes()
    (parent / "account-engine").symlink_to(external, target_is_directory=True)

    with pytest.raises(ConfigError, match="refusing symlink"):
        ClaudeAccountSwitcher()

    if sys.platform == "darwin":
        monkeypatch.setattr(sys, "argv", ["ccs-accounts", "--list", "--json"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["type"] == "ConfigError"
        assert "refusing symlink" in payload["error"]["message"]

    assert (parent / "account-engine").is_symlink()
    assert stat.S_IMODE(external.lstat().st_mode) == mode_before
    assert marker.read_bytes() == bytes_before


@pytest.mark.parametrize("child_name", ["configs", "credentials", "cache", "sessions"])
def test_symlinked_engine_child_refuses_constructor_without_touching_target(
    temp_home: Path, child_name: str,
):
    parent = _make_private_engine_parent(temp_home)
    root = parent / "account-engine"
    root.mkdir(mode=0o700)
    external = temp_home / f"external-{child_name}"
    external.mkdir(mode=0o750)
    marker = external / "sentinel"
    marker.write_bytes(b"do not modify")
    mode_before = stat.S_IMODE(external.lstat().st_mode)
    (root / child_name).symlink_to(external, target_is_directory=True)

    with pytest.raises(ConfigError, match="refusing symlink"):
        ClaudeAccountSwitcher()

    assert (root / child_name).is_symlink()
    assert stat.S_IMODE(external.lstat().st_mode) == mode_before
    assert marker.read_bytes() == b"do not modify"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs")
def test_dangerous_extended_acl_on_engine_root_is_refused(temp_home: Path):
    parent = _make_private_engine_parent(temp_home)
    root = parent / "account-engine"
    root.mkdir(mode=0o700)
    sentinel = root / "sentinel"
    sentinel.write_text("engine data", encoding="utf-8")
    mode_before = stat.S_IMODE(root.lstat().st_mode)
    subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read,write,delete", str(root)],
        check=True,
    )

    with pytest.raises(ConfigError, match="extended ACL"):
        ClaudeAccountSwitcher()

    assert root.is_dir()
    assert stat.S_IMODE(root.lstat().st_mode) == mode_before
    assert sentinel.read_text(encoding="utf-8") == "engine data"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs")
def test_deny_only_extended_acl_on_config_ancestor_is_accepted(temp_home: Path):
    config = temp_home / ".config"
    config.mkdir(mode=0o700)
    subprocess.run(
        ["/bin/chmod", "+a", "everyone deny delete", str(config)],
        check=True,
    )

    assert ClaudeAccountSwitcher().backup_dir == get_backup_root()


def test_symlinked_sequence_file_is_refused_before_read(temp_home: Path):
    parent = _make_private_engine_parent(temp_home)
    root = parent / "account-engine"
    root.mkdir(mode=0o700)
    external = temp_home / "external-sequence.json"
    external.write_text("external sequence sentinel", encoding="utf-8")
    sequence_link = root / "sequence.json"
    sequence_link.symlink_to(external)
    switcher = ClaudeAccountSwitcher()

    with pytest.raises(ConfigError, match="refusing symlink"):
        switcher._get_sequence_data()

    assert sequence_link.is_symlink()
    assert external.read_text(encoding="utf-8") == "external sequence sentinel"


def test_purge_refuses_symlinked_session_entry_before_scanning_target(
    temp_home: Path,
):
    parent = _make_private_engine_parent(temp_home)
    root = parent / "account-engine"
    root.mkdir(mode=0o700)
    sessions = root / "sessions"
    sessions.mkdir(mode=0o700)
    external = get_legacy_backup_root() / "profiles" / "legacy"
    external.mkdir(parents=True, mode=0o700)
    marker = external / ".session-marker"
    marker.write_text("legacy profile sentinel", encoding="utf-8")
    link = sessions / "1-user_example.com"
    link.symlink_to(external, target_is_directory=True)
    switcher = ClaudeAccountSwitcher()

    with pytest.raises(ConfigError, match="refusing symlink"):
        switcher.purge()

    assert link.is_symlink()
    assert marker.read_text(encoding="utf-8") == "legacy profile sentinel"
