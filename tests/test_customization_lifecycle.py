"""Generated customization data follows the managed profile's lifetime."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cc_swaper.profiles import ProfileStore
from cc_swaper.shared_mcp_plugins import prepare_shared_mcp_plugins
from cc_swaper.shared_settings import prepare_shared_settings


@pytest.mark.parametrize("purge_data", [False, True])
def test_profile_archive_or_purge_scopes_generated_overlays(
    tmp_path: Path, purge_data: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.delenv("CC_SWAPER_HOME", raising=False)
    store = ProfileStore(tmp_path / "store")
    store.add_default("main")
    profile = store.add_managed("work")
    assert profile.config_dir is not None
    main = Path.home() / ".claude"
    (main / "settings.json").write_text('{"verbose": true}', encoding="utf-8")
    (Path.home() / ".claude.json").write_text(json.dumps({
        "mcpServers": {"safe": {"type": "http", "url": "https://example.com/mcp"}},
        "oauthAccount": {"private": "not-copied"},
    }), encoding="utf-8")
    shared_marker = store.shared_projects / "keep.jsonl"
    shared_marker.write_text("shared", encoding="utf-8")

    settings_args, _ = prepare_shared_settings(store, profile)
    mcp_args, _ = prepare_shared_mcp_plugins(store, profile, tmp_path)
    generated = profile.config_dir / ".ccs-generated"
    assert generated.is_dir()
    assert Path(settings_args[1]).is_file()
    assert Path(mcp_args[1]).is_file()

    archive = store.home / "removed"
    archive.mkdir(mode=0o700)
    destination = archive / "work-20260924T000000Z-a1b2c3d4"
    store.remove_managed("work", archive_to=destination, purge_data=purge_data)
    if purge_data:
        shutil.rmtree(destination)
        store.finish_purge(destination)
        assert not destination.exists()
    else:
        assert (destination / ".ccs-generated").is_dir()
    assert not generated.exists()
    assert shared_marker.read_text(encoding="utf-8") == "shared"
    assert json.loads((Path.home() / ".claude.json").read_text())["oauthAccount"] == {
        "private": "not-copied"
    }
