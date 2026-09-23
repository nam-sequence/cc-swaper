from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from cc_swaper.profiles import Profile, ProfileStore
from cc_swaper.shared_mcp_plugins import prepare_shared_mcp_plugins


def _write_json(path: Path, payload: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)


def _managed_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ProfileStore, Profile, Path, Path]:
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(fake_home))
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir(mode=0o700)

    store = ProfileStore(tmp_path / "store")
    store.add_default("personal")
    profile = store.add_managed("work")
    cwd = tmp_path / "project"
    cwd.mkdir(mode=0o700)
    return store, profile, cwd, fake_home


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_default_profile_returns_no_shared_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, managed, cwd, _fake_home = _managed_profile(tmp_path, monkeypatch)
    default = store.get("personal")
    _write_json(Path.home() / ".claude.json", {
        "mcpServers": {"shared": {"command": "server"}},
        "oauthAccount": {"accessToken": "must-not-copy"},
    })

    args, env = prepare_shared_mcp_plugins(store, default, cwd)

    assert args == []
    assert env == {}
    assert managed.config_dir is not None
    assert not (managed.config_dir / ".ccs-generated").exists()


def test_managed_profile_copies_only_noncolliding_mcp_entries_privately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, profile, cwd, fake_home = _managed_profile(tmp_path, monkeypatch)
    secret = "do-not-print-or-copy-outside-mcp-map"
    _write_json(fake_home / ".claude.json", {
        "mcpServers": {
            "profile-collision": {"command": "main-profile"},
            "project-collision": {"command": "main-project"},
            "headers-helper": {
                "type": "http",
                "url": "https://example.invalid/mcp",
                "headersHelper": {"command": "read-secret", "env": {"KEY": secret}},
            },
            "shared": {
                "command": "mcp-server",
                "env": {"FIRECRAWL_API_URL": "https://api.firecrawl.dev"},
            },
        },
        "oauthAccount": {"accessToken": secret},
        "projects": {"private-transcript": secret},
        "trustedFolders": [secret],
    })
    assert profile.config_dir is not None
    _write_json(profile.config_dir / ".claude.json", {
        "mcpServers": {"profile-collision": {"command": "profile-server"}},
        "oauthAccount": {"accessToken": "profile-token"},
    })
    _write_json(cwd / ".mcp.json", {
        "mcpServers": {"project-collision": {"command": "project-server"}},
    }, mode=0o644)

    args, env = prepare_shared_mcp_plugins(store, profile, cwd)

    assert args[:1] == ["--mcp-config"]
    assert len(args) == 2
    snapshot = Path(args[1])
    assert profile.config_dir is not None
    assert snapshot == profile.config_dir / ".ccs-generated" / "mcp" / snapshot.name
    assert snapshot.is_file()
    assert _mode(snapshot) == 0o600
    assert _mode(snapshot.parent) == 0o700
    assert _mode(snapshot.parent.parent) == 0o700
    assert _mode(profile.config_dir / ".ccs-generated") == 0o700
    assert snapshot.name == f"v1-{hashlib.sha256(snapshot.read_bytes()).hexdigest()}.json"
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload == {
        "mcpServers": {
            "shared": {
                "command": "mcp-server",
                "env": {"FIRECRAWL_API_URL": "https://api.firecrawl.dev"},
            },
        },
    }
    assert "oauthAccount" not in payload
    assert "projects" not in payload
    assert "trustedFolders" not in payload
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert env == {}

    original = snapshot.read_bytes()
    _write_json(fake_home / ".claude.json", {"mcpServers": {}})
    next_args, _next_env = prepare_shared_mcp_plugins(store, profile, cwd)
    assert next_args == []
    assert snapshot.read_bytes() == original


def test_plugin_seed_uses_validated_main_root_and_keeps_account_plugins_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, profile, cwd, fake_home = _managed_profile(tmp_path, monkeypatch)
    assert profile.config_dir is not None
    main_plugins = fake_home / ".claude" / "plugins"
    main_plugins.mkdir(mode=0o700)
    (main_plugins / "known_marketplaces.json").write_text("{}\n", encoding="utf-8")
    (main_plugins / "known_marketplaces.json").chmod(0o600)
    (main_plugins / "marketplaces").mkdir(mode=0o700)
    (main_plugins / "cache").mkdir(mode=0o700)
    account_plugins = profile.config_dir / "plugins"
    account_plugins.mkdir(mode=0o700)
    account_marker = account_plugins / "account-only.txt"
    account_marker.write_text("keep", encoding="utf-8")

    args, env = prepare_shared_mcp_plugins(store, profile, cwd)

    assert args == []
    assert env == {"CLAUDE_CODE_PLUGIN_SEED_DIR": str(main_plugins)}
    assert env["CLAUDE_CODE_PLUGIN_SEED_DIR"] != str(account_plugins)
    assert "PLUGIN_CACHE_DIR" not in env
    assert account_marker.read_text(encoding="utf-8") == "keep"


def test_credential_bearing_shared_servers_are_not_copied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, profile, cwd, fake_home = _managed_profile(tmp_path, monkeypatch)
    _write_json(fake_home / ".claude.json", {
        "mcpServers": {
            "token-env": {
                "command": "token-server",
                "env": {"service_token": "env-secret-value"},
            },
            "credentials-env": {
                "command": "credentials-server",
                "env": {"GOOGLE_APPLICATION_CREDENTIALS": "/path/to/key.json"},
            },
            "askpass-env": {
                "command": "askpass-server",
                "env": {"GIT_ASKPASS": "askpass-secret-value"},
            },
            "generic-options-env": {
                "command": "options-server",
                "env": {"OPTIONS": "generic-secret-value"},
            },
            "credential-url-env": {
                "command": "firecrawl-mcp",
                "env": {
                    "FIRECRAWL_API_URL": "https://user:pass@example.invalid/mcp",
                },
            },
            "fragment-url-env": {
                "command": "firecrawl-mcp",
                "env": {"FIRECRAWL_API_URL": "https://api.firecrawl.dev/#secret"},
            },
            "query-url-env": {
                "command": "firecrawl-mcp",
                "env": {
                    "FIRECRAWL_API_URL": "https://api.firecrawl.dev/?api_key=url-secret-value",
                },
            },
            "bearer-header": {
                "type": "http",
                "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer header-secret-value"},
            },
            "api-key-argument": {
                "command": "mcp-server",
                "args": ["--api-key", "argument-secret-value"],
            },
            "short-header-argument": {
                "command": "mcp-server",
                "args": ["-H", "X-API-Key: short-header-secret-value"],
            },
            "short-userpass-argument": {
                "command": "mcp-server",
                "args": ["-u", "user:short-password-value"],
            },
            "credential-url-argument": {
                "command": "mcp-server",
                "args": ["https://example.invalid/mcp?access_token=arg-url-secret-value"],
            },
            "token-argument": {
                "command": "mcp-server",
                "args": ["--token=inline-secret-value"],
            },
            "credential-assignment": {
                "command": "API_SECRET=assignment-secret-value mcp-server",
            },
            "credential-userinfo": {
                "type": "http",
                "url": "https://user:password-value@example.invalid/mcp",
            },
            "credential-query": {
                "type": "http",
                "url": "https://example.invalid/mcp?access_token=query-secret-value",
            },
            "pencil": {"command": "pencil-mcp"},
            "mobbin": {"command": "mobbin-mcp", "env": {}},
            "firecrawl": {
                "command": "firecrawl-mcp",
                "env": {"FIRECRAWL_API_URL": "https://api.firecrawl.dev"},
            },
        },
    })

    args, _env = prepare_shared_mcp_plugins(store, profile, cwd)

    assert args[:1] == ["--mcp-config"]
    snapshot = Path(args[1])
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["mcpServers"] == {
        "firecrawl": {
            "command": "firecrawl-mcp",
            "env": {"FIRECRAWL_API_URL": "https://api.firecrawl.dev"},
        },
        "mobbin": {"command": "mobbin-mcp", "env": {}},
        "pencil": {"command": "pencil-mcp"},
    }
    snapshot_text = snapshot.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    for secret in (
        "env-secret-value",
        "askpass-secret-value",
        "generic-secret-value",
        "url-secret-value",
        "header-secret-value",
        "argument-secret-value",
        "short-header-secret-value",
        "short-password-value",
        "arg-url-secret-value",
        "inline-secret-value",
        "assignment-secret-value",
        "password-value",
        "query-secret-value",
    ):
        assert secret not in snapshot_text
        assert secret not in captured.out
        assert secret not in captured.err


def test_deep_project_mcp_json_returns_sanitized_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, profile, cwd, fake_home = _managed_profile(tmp_path, monkeypatch)
    _write_json(fake_home / ".claude.json", {
        "mcpServers": {"safe": {"command": "safe"}},
    })
    project_config = cwd / ".mcp.json"
    project_config.write_text("[" * 1200 + "0" + "]" * 1200, encoding="utf-8")
    project_config.chmod(0o600)

    with pytest.raises(ValueError, match=r"^invalid JSON in \.mcp\.json$") as error:
        prepare_shared_mcp_plugins(_store, profile, cwd)

    assert str(error.value) == "invalid JSON in .mcp.json"


@pytest.mark.parametrize("git_marker_kind", ["directory", "worktree-file"])
def test_nested_git_cwd_uses_project_root_mcp_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_marker_kind: str,
) -> None:
    store, profile, _cwd, fake_home = _managed_profile(tmp_path, monkeypatch)
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    marker = repository / ".git"
    if git_marker_kind == "directory":
        marker.mkdir(mode=0o700)
    else:
        marker.write_text("gitdir: /tmp/worktrees/project\n", encoding="utf-8")
        marker.chmod(0o600)
    nested_cwd = repository / "src" / "nested"
    nested_cwd.mkdir(parents=True, mode=0o700)
    _write_json(fake_home / ".claude.json", {
        "mcpServers": {
            "project-server": {"command": "main-account-version"},
            "safe-server": {"command": "safe"},
        },
    })
    _write_json(repository / ".mcp.json", {
        "mcpServers": {"project-server": {"command": "project-version"}},
    })

    args, _env = prepare_shared_mcp_plugins(store, profile, nested_cwd)

    assert args[:1] == ["--mcp-config"]
    payload = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    assert payload["mcpServers"] == {"safe-server": {"command": "safe"}}


@pytest.mark.parametrize("profile_project_key", ["cwd", "git-root"])
def test_profile_project_local_mcp_servers_exclude_shared_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_project_key: str,
) -> None:
    store, profile, _cwd, fake_home = _managed_profile(tmp_path, monkeypatch)
    assert profile.config_dir is not None
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o700)
    (repository / ".git").mkdir(mode=0o700)
    nested_cwd = repository / "src" / "nested"
    nested_cwd.mkdir(parents=True, mode=0o700)
    local_key_path = nested_cwd if profile_project_key == "cwd" else repository
    _write_json(fake_home / ".claude.json", {
        "mcpServers": {
            "profile-project-server": {"command": "main-account-version"},
            "safe-server": {"command": "safe"},
        },
    })
    _write_json(profile.config_dir / ".claude.json", {
        "projects": {
            str(local_key_path): {
                "mcpServers": {
                    "profile-project-server": {"command": "profile-local-version"},
                },
            },
        },
    })

    args, _env = prepare_shared_mcp_plugins(store, profile, nested_cwd)

    assert args[:1] == ["--mcp-config"]
    payload = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    assert payload["mcpServers"] == {"safe-server": {"command": "safe"}}


def test_incomplete_plugin_root_is_not_used_as_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, profile, cwd, fake_home = _managed_profile(tmp_path, monkeypatch)
    plugins = fake_home / ".claude" / "plugins"
    plugins.mkdir(mode=0o700)
    (plugins / "known_marketplaces.json").write_text("{}\n", encoding="utf-8")
    (plugins / "marketplaces").mkdir(mode=0o700)

    args, env = prepare_shared_mcp_plugins(store, profile, cwd)

    assert args == []
    assert env == {}


def test_unsafe_project_config_fails_closed_without_revealing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, profile, cwd, fake_home = _managed_profile(tmp_path, monkeypatch)
    secret = "project-secret-value"
    _write_json(fake_home / ".claude.json", {
        "mcpServers": {"shared": {"command": "server", "env": {"TOKEN": secret}}},
    })
    target = tmp_path / "outside.json"
    _write_json(target, {"mcpServers": {"shared": {"command": "local"}}})
    (cwd / ".mcp.json").symlink_to(target)

    with pytest.raises(ValueError, match=r"cannot safely inspect \.mcp\.json") as error:
        prepare_shared_mcp_plugins(store, profile, cwd)

    assert secret not in str(error.value)
    assert profile.config_dir is not None
    assert not (profile.config_dir / ".ccs-generated").exists()
