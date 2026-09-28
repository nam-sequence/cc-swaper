"""Tests for claude_swap.paths resolver helpers.

These tests verify that cswap resolves Claude Code config/credential paths the
same way claude-code itself does. If these drift from claude-code's behavior,
cswap will read the wrong files and misattribute accounts (see issue #16).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.paths import (
    LEGACY_BACKUP_DIRNAME,
    get_backup_root,
    get_claude_config_home,
    get_credentials_path,
    get_global_config_path,
    get_legacy_backup_root,
    migrate_legacy_backup_dir,
)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Temp HOME with CLAUDE_CONFIG_DIR unset."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    with patch("pathlib.Path.home", return_value=home):
        yield home


class TestGetClaudeConfigHome:
    def test_default_is_dot_claude_in_home(self, isolated_home: Path):
        assert get_claude_config_home() == isolated_home / ".claude"

    def test_respects_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        custom = tmp_path / "custom-claude"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert get_claude_config_home() == custom


class TestGetGlobalConfigPath:
    def test_default_returns_homedir_claude_json(self, isolated_home: Path):
        """Without CCD, claude-code writes .claude.json at $HOME, not inside .claude/."""
        assert get_global_config_path() == isolated_home / ".claude.json"

    def test_ccd_set_returns_ccd_claude_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        custom = tmp_path / "ccd"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert get_global_config_path() == custom / ".claude.json"

    def test_legacy_config_json_takes_precedence(self, isolated_home: Path):
        """If ~/.claude/.config.json exists, claude-code uses that (legacy)."""
        config_home = isolated_home / ".claude"
        config_home.mkdir(exist_ok=True)
        legacy = config_home / ".config.json"
        legacy.write_text("{}")
        assert get_global_config_path() == legacy

    def test_legacy_config_json_in_ccd_takes_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        custom = tmp_path / "ccd"
        custom.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        legacy = custom / ".config.json"
        legacy.write_text("{}")
        assert get_global_config_path() == legacy


class TestGetCredentialsPath:
    def test_default_inside_dot_claude(self, isolated_home: Path):
        assert get_credentials_path() == isolated_home / ".claude" / ".credentials.json"

    def test_respects_ccd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        custom = tmp_path / "ccd"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
        assert get_credentials_path() == custom / ".credentials.json"


class TestGetBackupRoot:
    def test_engine_has_its_own_private_namespace(self, isolated_home: Path):
        root = get_backup_root()
        assert root == isolated_home / ".config" / "cc-swaper" / "account-engine"
        assert root != isolated_home / LEGACY_BACKUP_DIRNAME
        assert root != isolated_home / ".config" / "cc-swaper" / "profiles"

    def test_xdg_override_does_not_move_engine_store(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("XDG_DATA_HOME", str(isolated_home / "xdg"))
        assert get_backup_root() == isolated_home / ".config" / "cc-swaper" / "account-engine"

    def test_legacy_helper_names_upstream_store_for_diagnostics_only(
        self, isolated_home: Path
    ):
        assert get_legacy_backup_root() == isolated_home / LEGACY_BACKUP_DIRNAME


class TestMigrateLegacyBackupDir:
    def test_legacy_migration_is_noop_without_inspecting_upstream_store(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        legacy = isolated_home / LEGACY_BACKUP_DIRNAME
        legacy.mkdir()
        marker = legacy / "sequence.json"
        marker.write_text('{"external": true}')
        target = get_backup_root()

        original_exists = Path.exists

        def deny_legacy_stat(path: Path) -> bool:
            if path == legacy or legacy in path.parents:
                raise AssertionError("the external cswap store was inspected")
            return original_exists(path)

        monkeypatch.setattr(Path, "exists", deny_legacy_stat)
        assert migrate_legacy_backup_dir(target) is False
        assert marker.read_text() == '{"external": true}'
        assert original_exists(target) is False
