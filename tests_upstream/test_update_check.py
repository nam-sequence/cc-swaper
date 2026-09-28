"""Tests for update_check module."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from claude_swap.update_check import (
    CACHE_TTL,
    _detect_install_method,
    check_for_update,
    run_self_upgrade,
)


def _make_release_response(version: str):
    from unittest.mock import MagicMock

    data = json.dumps({"tag_name": f"v{version}"}).encode()
    mock_resp = MagicMock()
    mock_resp.read.return_value = data
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _write_cache(path, version, timestamp=None):
    """Write a cache file in the shared cache format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "timestamp": timestamp if timestamp is not None else time.time(),
        "data": version,
    }))


class TestCheckForUpdate:
    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_newer_version_available(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "0.4.0" in result
        assert "0.3.2" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_already_on_latest(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.3.2")

        result = check_for_update("0.3.2")

        assert result is None

    @patch("claude_swap.update_check.urllib.request.urlopen", side_effect=OSError("network error"))
    def test_network_error_returns_none_and_caches(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        result = check_for_update("0.3.2")

        assert result is None
        assert cache_path.exists()
        cache = json.loads(cache_path.read_text())
        assert cache["data"] is None

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_fresh_error_cache_skips_network(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, None)
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        result = check_for_update("0.3.2")

        mock_urlopen.assert_not_called()
        assert result is None

    def test_fresh_cache_no_network(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "0.5.0")
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)

        with patch("claude_swap.update_check.urllib.request.urlopen") as mock_urlopen:
            result = check_for_update("0.3.2")
            mock_urlopen.assert_not_called()

        assert result is not None
        assert "0.5.0" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_stale_cache_fetches_from_github_release_feed(self, mock_urlopen, tmp_path, monkeypatch):
        cache_path = tmp_path / "cache.json"
        _write_cache(cache_path, "0.3.0", timestamp=time.time() - CACHE_TTL - 1)
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", cache_path)
        mock_urlopen.return_value = _make_release_response("0.4.0")

        result = check_for_update("0.3.2")

        mock_urlopen.assert_called_once()
        assert result is not None
        assert "0.4.0" in result


class TestCheckForUpdatePrereleases:
    """cc-swaper can compare prerelease tags from GitHub releases as a pre-release first, so the version
    cli.main hands to check_for_update is routinely something like 0.27.0b1."""

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_later_release(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.28.0")

        result = check_for_update("0.27.0b1")

        assert result is not None
        assert "0.28.0" in result
        assert "0.27.0b1" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_its_own_final_release(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.27.0")

        result = check_for_update("0.27.0b1")

        assert result is not None
        assert "0.27.0" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_prerelease_is_told_about_later_prerelease(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.27.0rc1")

        result = check_for_update("0.27.0b2")

        assert result is not None
        assert "0.27.0rc1" in result

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_final_release_is_not_pushed_onto_a_prerelease(
        self, mock_urlopen, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.28.0b1")

        assert check_for_update("0.27.0") is None

    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_unparseable_version_stays_silent(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        mock_urlopen.return_value = _make_release_response("0.28.0")

        assert check_for_update("main") is None


class TestDetectInstallMethod:
    def _set_prefix(self, monkeypatch, prefix: str) -> None:
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", prefix)
        # Clear env vars by default so path-based detection runs in isolation.
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)

    def test_uv_tool_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/share/uv/tools/claude-swap")
        assert _detect_install_method() == "uv"

    def test_pipx_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/pipx/venvs/claude-swap")
        assert _detect_install_method() == "pipx"

    def test_non_adjacent_uv_tools_does_not_match(self, monkeypatch):
        # Both segments present but not adjacent — must not false-positive.
        self._set_prefix(monkeypatch, "/home/me/projects/uv/some-tools/.venv")
        assert _detect_install_method() is None

    def test_non_adjacent_pipx_venvs_does_not_match(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/repos/pipx-clone/venvs-of-mine/.venv")
        assert _detect_install_method() is None

    def test_source_checkout_returns_none(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/code/claude-swap/.venv")
        assert _detect_install_method() is None

    def test_mixed_case_path_detected(self, monkeypatch):
        # Lowercasing should make matching case-insensitive (e.g. Windows).
        self._set_prefix(monkeypatch, "/Home/Me/.local/share/UV/Tools/claude-swap")
        assert _detect_install_method() == "uv"

    def test_uv_tool_dir_env_with_prefix_under_it(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "uv-tools"
        prefix = custom_root / "claude-swap"
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", str(prefix))
        monkeypatch.setenv("UV_TOOL_DIR", str(custom_root))
        monkeypatch.delenv("PIPX_HOME", raising=False)
        assert _detect_install_method() == "uv"

    def test_uv_tool_dir_env_set_but_prefix_elsewhere(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "uv-tools"
        # Prefix lives somewhere else entirely — env var alone must not trigger.
        monkeypatch.setattr(
            "claude_swap.update_check.sys.prefix", str(tmp_path / "some-project" / ".venv")
        )
        monkeypatch.setenv("UV_TOOL_DIR", str(custom_root))
        monkeypatch.delenv("PIPX_HOME", raising=False)
        assert _detect_install_method() is None

    def test_pipx_home_env_with_prefix_under_it(self, monkeypatch, tmp_path):
        custom_root = tmp_path / "pipx-home"
        prefix = custom_root / "venvs" / "claude-swap"
        monkeypatch.setattr("claude_swap.update_check.sys.prefix", str(prefix))
        monkeypatch.setenv("PIPX_HOME", str(custom_root))
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        assert _detect_install_method() == "pipx"


class TestCheckForUpdateMessage:
    @patch("claude_swap.update_check.urllib.request.urlopen")
    def test_message_points_to_bundled_release_page(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_swap.update_check.CACHE_PATH", tmp_path / "cache.json")
        monkeypatch.setattr("claude_swap.update_check._detect_install_method", lambda: "uv")
        mock_urlopen.return_value = _make_release_response("0.4.0")

        result = check_for_update("0.3.2")

        assert result is not None
        assert "cc-swaper" in result
        assert "https://github.com/nam-sequence/cc-swaper/releases/latest" in result
        assert "claude-swap" not in result


class TestRunSelfUpgrade:
    def test_refuses_external_self_upgrade(self, capsys):
        with patch("claude_swap.update_check._detect_install_method") as detect:
            assert run_self_upgrade() == 1

        detect.assert_not_called()
        err = capsys.readouterr().err
        assert "will not upgrade the separate upstream claude-swap installation" in err
        assert "https://github.com/nam-sequence/cc-swaper/releases/latest" in err
