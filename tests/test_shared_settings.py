from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from cc_swaper.profiles import Profile, ProfileStore
from cc_swaper.shared_settings import prepare_shared_settings


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _store(tmp_path: Path) -> tuple[ProfileStore, Profile]:
    store = ProfileStore(tmp_path / "store")
    store.add_default("personal")
    return store, store.add_managed("work")


def _main_claude() -> Path:
    return Path.home() / ".claude"


def test_default_profile_needs_no_shared_settings_overlay(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "store")
    default = store.add_default("personal")

    assert prepare_shared_settings(store, default) == ([], {})


def test_empty_main_profile_has_no_shared_arguments_or_environment(
    tmp_path: Path,
) -> None:
    store, managed = _store(tmp_path)

    assert prepare_shared_settings(store, managed) == ([], {})


def test_nonempty_settings_without_resources_has_no_add_dir_or_env(
    tmp_path: Path,
) -> None:
    store, managed = _store(tmp_path)
    settings_path = _main_claude() / "settings.json"
    settings_path.write_text('{"effortLevel":"high"}', encoding="utf-8")

    args, env = prepare_shared_settings(store, managed)

    assert args[0] == "--settings"
    overlay = json.loads(Path(args[1]).read_text(encoding="utf-8"))
    assert overlay == {"effortLevel": "high"}
    assert env == {}
    assert "--add-dir" not in args


def test_existing_generated_directory_with_unsafe_mode_is_not_changed(
    tmp_path: Path,
) -> None:
    store, managed = _store(tmp_path)
    assert managed.config_dir is not None
    generated = managed.config_dir / ".ccs-generated"
    generated.mkdir(mode=0o700)
    generated.chmod(0o755)

    with pytest.raises(ValueError, match="not private"):
        prepare_shared_settings(store, managed)

    assert _mode(generated) == 0o755


def test_managed_profile_gets_private_filtered_settings_and_resource_view(
    tmp_path: Path,
) -> None:
    store, managed = _store(tmp_path)
    main = _main_claude()
    main.mkdir(exist_ok=True)
    shared_settings = {
        "theme": "shared-theme",
        "model": "shared-model",
        "agentPushNotifEnabled": True,
        "effortLevel": "high",
        "enabledPlugins": {"example@local": True},
        "extraKnownMarketplaces": {
            "main-tools": {
                "source": {
                    "source": "url",
                    "url": "https://github.com/example/tools.git?ref=main",
                },
            },
            "private-userinfo": {
                "source": {
                    "source": "url",
                    "url": "https://alice:private-pass@example.com/marketplace.git",
                },
            },
            "private-query-key": {
                "source": {
                    "source": "url",
                    "url": "https://example.com/marketplace.git?access_token=private-value",
                },
            },
            "private-query-value": {
                "source": {
                    "source": "url",
                    "url": "https://example.com/marketplace.git?ref=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                },
            },
        },
        "outputStyle": "concise",
        "permissions": {"allow": ["Read"]},
        "apiKeyHelper": "/path/to/shared/api-key-helper",
        "skipDangerousModePermissionPrompt": True,
        "crossSessionInbound": "accept",
        "remoteControlAtStartup": True,
        "hasCompletedOnboarding": True,
        "unknownFutureSetting": "must not inherit",
        "env": {
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet",
            "SHARED_SAFE": "not on the allowlist",
            "ANTHROPIC_API_KEY": "secret-api-key",
            "ANTHROPIC_CUSTOM_HEADERS": "Authorization: secret",
            "CLAUDE_CODE_OAUTH_TOKEN": "secret-oauth-token",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_CLIENT_CUSTOM": "credential-override",
            "CLAUDE_CODE_SKIP_LOGIN_AUTH": "1",
            "CLAUDE_CONFIG_DIR": "/wrong/config",
            "AWS_BEARER_TOKEN_BEDROCK": "secret-bedrock-token",
            "NODE_OPTIONS": "--require ./unexpected.js",
        },
    }
    main_settings_path = main / "settings.json"
    main_settings_path.write_text(json.dumps(shared_settings), encoding="utf-8")
    assert managed.config_dir is not None
    managed_settings_path = managed.config_dir / "settings.json"
    managed_original = '{"theme":"managed-theme","model":"managed-model"}\n'
    managed_settings_path.write_text(managed_original, encoding="utf-8")

    sources = {
        main / "CLAUDE.md": "shared project instructions\n",
        main / "rules" / "quality.md": "shared rule\n",
        main / "skills" / "custom-review" / "SKILL.md": "authored skill\n",
        main / "skills" / "synced" / "vendor-skill" / "SKILL.md": "synced skill\n",
        main / "commands" / "review.md": "review command\n",
        main / "agents" / "reviewer.md": "review agent\n",
        main / "credentials.json": '{"token":"do not copy"}',
        main / "cache" / "session.json": '{"state":"do not copy"}',
    }
    for path, content in sources.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    linked_skill = Path.home() / ".agents" / "skills" / "shared-assistant"
    linked_skill.mkdir(parents=True)
    (linked_skill / "SKILL.md").write_text("linked authored skill\n", encoding="utf-8")
    authored_link = main / "skills" / "linked-assistant"
    authored_link.symlink_to(linked_skill, target_is_directory=True)
    original_sources = {path: path.read_bytes() for path in sources}
    authored_link_target = authored_link.readlink()
    main_original = main_settings_path.read_bytes()

    args, env = prepare_shared_settings(store, managed)

    settings_snapshot = Path(args[args.index("--settings") + 1])
    view = Path(args[args.index("--add-dir") + 1])
    assert args == ["--settings", str(settings_snapshot), "--add-dir", str(view)]
    assert settings_snapshot == (
        managed.config_dir / ".ccs-generated" / "settings-and-resources"
        / "settings" / settings_snapshot.name
    )
    assert view == (
        managed.config_dir / ".ccs-generated" / "settings-and-resources"
        / "resources" / view.name
    )
    assert env == {"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1"}

    overlay = json.loads(settings_snapshot.read_text(encoding="utf-8"))
    assert overlay == {
        "agentPushNotifEnabled": True,
        "effortLevel": "high",
        "enabledPlugins": {"example@local": True},
        "extraKnownMarketplaces": {
            "main-tools": {
                "source": {
                    "source": "url",
                    "url": "https://github.com/example/tools.git?ref=main",
                },
            },
        },
        "env": {
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
            "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet",
        },
        "outputStyle": "concise",
    }
    assert _mode(settings_snapshot) == 0o600
    assert _mode(settings_snapshot.parent) == 0o700
    assert _mode(view) == 0o700
    assert (view / "CLAUDE.md").read_text(encoding="utf-8") == "shared project instructions\n"
    assert (view / ".claude" / "rules" / "quality.md").read_text() == "shared rule\n"
    assert (
        view / ".claude" / "skills" / "custom-review" / "SKILL.md"
    ).read_text() == "authored skill\n"
    assert (
        view / ".claude" / "skills" / "linked-assistant" / "SKILL.md"
    ).read_text() == "linked authored skill\n"
    assert (view / ".claude" / "commands" / "review.md").read_text() == "review command\n"
    assert (view / ".claude" / "agents" / "reviewer.md").read_text() == "review agent\n"
    assert not (view / ".claude" / "skills" / "synced").exists()
    assert not (view / "credentials.json").exists()
    assert not (view / "cache").exists()
    assert all(_mode(path) == 0o600 for path in view.rglob("*") if path.is_file())
    assert all(_mode(path) == 0o700 for path in view.rglob("*") if path.is_dir())

    # Shared and managed sources stay byte-for-byte unchanged; private values
    # from the shared env block do not appear in the generated settings file.
    assert main_settings_path.read_bytes() == main_original
    assert managed_settings_path.read_text(encoding="utf-8") == managed_original
    assert {path: path.read_bytes() for path in sources} == original_sources
    assert authored_link.is_symlink()
    assert authored_link.readlink() == authored_link_target
    snapshot_text = settings_snapshot.read_text(encoding="utf-8")
    for secret in (
        "secret-api-key",
        "secret-oauth-token",
        "secret-bedrock-token",
        "credential-override",
    ):
        assert secret not in snapshot_text
    for excluded_key in (
        "permissions",
        "apiKeyHelper",
        "skipDangerousModePermissionPrompt",
        "crossSessionInbound",
        "remoteControlAtStartup",
        "hasCompletedOnboarding",
        "unknownFutureSetting",
        "SHARED_SAFE",
    ):
        assert excluded_key not in overlay and excluded_key not in snapshot_text
    for marketplace_secret in (
        "private-pass",
        "private-value",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "private-userinfo",
        "private-query-key",
        "private-query-value",
    ):
        assert marketplace_secret not in snapshot_text


def test_repeated_preparation_is_idempotent_and_old_views_remain_usable(
    tmp_path: Path,
) -> None:
    store, managed = _store(tmp_path)
    main = _main_claude()
    main.mkdir(exist_ok=True)
    instructions = main / "CLAUDE.md"
    instructions.write_text("version one\n", encoding="utf-8")

    first = prepare_shared_settings(store, managed)
    first_view = Path(first[0][first[0].index("--add-dir") + 1])
    first_bytes = (first_view / "CLAUDE.md").read_bytes()
    second = prepare_shared_settings(store, managed)
    assert second == first
    assert (first_view / "CLAUDE.md").read_bytes() == first_bytes

    instructions.write_text("version two\n", encoding="utf-8")
    third = prepare_shared_settings(store, managed)
    third_view = Path(third[0][third[0].index("--add-dir") + 1])
    assert third_view != first_view
    assert (first_view / "CLAUDE.md").read_text() == "version one\n"
    assert (third_view / "CLAUDE.md").read_text() == "version two\n"
    assert first_view.is_dir() and third_view.is_dir()


def test_modified_resource_copy_is_rebuilt_without_changing_active_view(
    tmp_path: Path,
) -> None:
    store, managed = _store(tmp_path)
    main = _main_claude()
    main.mkdir(exist_ok=True)
    (main / "CLAUDE.md").write_text("source instructions\n", encoding="utf-8")

    first_args, _ = prepare_shared_settings(store, managed)
    first_view = Path(first_args[first_args.index("--add-dir") + 1])
    (first_view / "CLAUDE.md").write_text("edited by active session\n", encoding="utf-8")

    second_args, _ = prepare_shared_settings(store, managed)
    second_view = Path(second_args[second_args.index("--add-dir") + 1])
    assert second_view != first_view
    assert (first_view / "CLAUDE.md").read_text() == "edited by active session\n"
    assert (second_view / "CLAUDE.md").read_text() == "source instructions\n"

    third_args, _ = prepare_shared_settings(store, managed)
    assert third_args == second_args


@pytest.mark.parametrize(
    "raw, message",
    [
        (b"[]", "must be an object"),
        (b'{"theme":"one","theme":"two"}', "invalid shared settings JSON"),
        (b'{"theme":NaN}', "invalid shared settings JSON"),
        (b"{" + b" " * (2 * 1024 * 1024), "exceeds 2 MiB"),
    ],
)
def test_shared_settings_require_bounded_strict_json_object(
    tmp_path: Path, raw: bytes, message: str
) -> None:
    store, managed = _store(tmp_path)
    main_settings = _main_claude() / "settings.json"
    main_settings.write_bytes(raw)

    with pytest.raises(ValueError, match=message):
        prepare_shared_settings(store, managed)


def test_unsafe_symlinks_in_authored_resources_are_rejected(tmp_path: Path) -> None:
    store, managed = _store(tmp_path)
    main = _main_claude()
    skill = main / "skills" / "unsafe"
    skill.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside resource", encoding="utf-8")
    try:
        (skill / "SKILL.md").symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="unsafe"):
        prepare_shared_settings(store, managed)
    assert outside.read_text(encoding="utf-8") == "outside resource"


def test_authored_skill_link_outside_home_is_rejected(tmp_path: Path) -> None:
    store, managed = _store(tmp_path)
    main = _main_claude()
    skills = main / "skills"
    skills.mkdir()
    outside_skill = tmp_path / "external-skill"
    outside_skill.mkdir()
    (outside_skill / "SKILL.md").write_text("outside", encoding="utf-8")
    try:
        (skills / "external").symlink_to(outside_skill, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="authored skill symlink target"):
        prepare_shared_settings(store, managed)


def test_dangling_in_home_authored_skill_link_is_skipped(tmp_path: Path) -> None:
    store, managed = _store(tmp_path)
    main = _main_claude()
    skills = main / "skills"
    skills.mkdir()
    (main / "CLAUDE.md").write_text("still share this file", encoding="utf-8")
    missing_target = Path.home() / ".agents" / "skills" / "removed-skill"
    try:
        (skills / "removed-skill").symlink_to(missing_target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    args, _ = prepare_shared_settings(store, managed)
    view = Path(args[args.index("--add-dir") + 1])
    assert (view / "CLAUDE.md").read_text() == "still share this file"
    assert not (view / ".claude" / "skills" / "removed-skill").exists()
