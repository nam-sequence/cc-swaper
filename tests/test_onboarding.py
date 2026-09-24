"""Repair the first-run login loop only for a signed-in managed profile."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from cc_swaper import onboarding
from cc_swaper.profiles import Profile, ProfileStore


def _profile(tmp_path: Path) -> tuple[Profile, Path]:
    store = ProfileStore(tmp_path / "store")
    store.add_default("main")
    profile = store.add_managed("work")
    assert profile.config_dir is not None
    return profile, profile.config_dir / ".claude.json"


def _valid_status(profile: Profile) -> dict[str, object]:
    return {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "configDirectory": str(profile.config_dir),
    }


def test_repair_adds_only_marker_and_preserves_credential_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, path = _profile(tmp_path)
    original = b'{"oauthAccount":{"id":"private"},"projects":{"/work":{"trusted":true}}}\n'
    path.write_bytes(original)
    path.chmod(0o600)
    assert profile.config_dir is not None
    credentials = profile.config_dir / ".credentials.json"
    credentials.write_bytes(b"credential bytes")
    credentials.chmod(0o600)
    monkeypatch.setattr(onboarding, "auth_details", lambda *_args: _valid_status(profile))

    assert onboarding.needs_repair(profile)
    assert onboarding.repair_if_authenticated(profile, "/fake/claude")
    assert not onboarding.needs_repair(profile)
    assert not onboarding.repair_if_authenticated(profile, "/fake/claude")
    assert path.read_bytes() == original[:-2] + b',"hasCompletedOnboarding":true}\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert credentials.read_bytes() == b"credential bytes"


@pytest.mark.parametrize(
    "status",
    [
        None,
        {"loggedIn": False},
        {"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty"},
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "configDirectory": "/other"},
    ],
)
def test_repair_requires_matching_first_party_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: dict[str, object] | None,
) -> None:
    profile, path = _profile(tmp_path)
    original = b'{"oauthAccount":{"id":"private"}}'
    path.write_bytes(original)
    path.chmod(0o600)
    monkeypatch.setattr(onboarding, "auth_details", lambda *_args: status)

    assert not onboarding.repair_if_authenticated(profile, "/fake/claude")
    assert path.read_bytes() == original


def test_false_marker_preserves_other_json_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, path = _profile(tmp_path)
    original = {
        "hasCompletedOnboarding": False,
        "oauthAccount": {"id": "private"},
        "projects": {"/work": {"mcpServers": {"local": {"command": "true"}}}},
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(onboarding, "auth_details", lambda *_args: _valid_status(profile))

    assert onboarding.repair_if_authenticated(profile, "/fake/claude")
    original["hasCompletedOnboarding"] = True
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_concurrent_change_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, path = _profile(tmp_path)
    path.write_text('{"oauthAccount":{"id":"old"}}', encoding="utf-8")
    path.chmod(0o600)

    def concurrent_change(*_args: object) -> dict[str, object]:
        path.write_text('{"oauthAccount":{"id":"new"}}', encoding="utf-8")
        return _valid_status(profile)

    monkeypatch.setattr(onboarding, "auth_details", concurrent_change)
    with pytest.raises(RuntimeError, match="changed during onboarding repair"):
        onboarding.repair_if_authenticated(profile, "/fake/claude")
    assert json.loads(path.read_text())["oauthAccount"]["id"] == "new"
    assert list(path.parent.glob(".claude.json.ccs-*")) == []


def test_unsafe_or_invalid_state_is_not_rewritten(tmp_path: Path) -> None:
    profile, path = _profile(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="unsafe Claude profile state"):
        onboarding.needs_repair(profile)
    path.unlink()
    path.write_text('{"oauthAccount":{},"oauthAccount":{}}', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="invalid Claude profile state JSON"):
        onboarding.needs_repair(profile)
    path.write_text('{"hasCompletedOnboarding":"yes"}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid Claude onboarding marker"):
        onboarding.needs_repair(profile)


def test_missing_state_file_is_not_created(tmp_path: Path) -> None:
    profile, path = _profile(tmp_path)
    assert not onboarding.needs_repair(profile)
    assert not onboarding.repair_if_authenticated(profile, "/fake/claude")
    assert not path.exists()
