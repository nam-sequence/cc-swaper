from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import time
from pathlib import Path

import pytest

from cc_swaper.profiles import ProfileStore
from cc_swaper import service


def test_launchagent_install_and_uninstall_use_private_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProfileStore(tmp_path / "store")
    agents_dir = tmp_path / "LaunchAgents"
    plist = agents_dir / f"{service.LABEL}.plist"
    fake_ccs = tmp_path / "ccs"
    fake_ccs.write_text("#!/bin/sh\n")
    fake_ccs.chmod(0o755)
    monkeypatch.setattr(service, "_plist_path", lambda: plist)
    monkeypatch.setattr(service, "_trusted_ccs_path", lambda: fake_ccs)
    loaded = False
    calls: list[list[str]] = []

    def fake_loaded() -> bool:
        return loaded

    def fake_run(args, **_kwargs):
        nonlocal loaded
        calls.append(list(args))
        if "bootstrap" in args:
            loaded = True
        if "bootout" in args:
            loaded = False
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_loaded", fake_loaded)
    monkeypatch.setattr(service.subprocess, "run", fake_run)
    permissive_log = store.home / "monitor.stdout.log"
    permissive_log.write_text("")
    permissive_log.chmod(0o666)
    assert service.install(store) == plist
    payload = plistlib.loads(plist.read_bytes())
    assert payload["Label"] == service.LABEL
    assert payload["ProgramArguments"] == [str(fake_ccs), "service", "run"]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["EnvironmentVariables"]["CC_SWAPER_HOME"] == str(store.home)
    assert payload["EnvironmentVariables"]["CC_SWAPER_TMUX_SOCKET"] == "cc-swaper"
    assert stat.S_IMODE(plist.stat().st_mode) == 0o644
    assert stat.S_IMODE((store.home / "monitor.stdout.log").stat().st_mode) == 0o600
    assert any("bootstrap" in call for call in calls)

    assert service.uninstall(store) == plist
    assert not plist.exists()
    assert any("bootout" in call for call in calls)


def test_service_status_requires_fresh_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProfileStore(tmp_path / "store")
    monkeypatch.setattr(service, "_loaded", lambda: True)
    service._write_snapshot(store, {
        "pid": os.getpid(), "updated_at": time.time(), "sessions": [{"name": "ccs-demo"}]
    })
    current = service.status(store)
    assert current["healthy"] is True
    assert current["sessions"][0]["name"] == "ccs-demo"

    service._write_snapshot(store, {
        "pid": os.getpid(), "updated_at": time.time() - 60, "sessions": []
    })
    assert service.status(store)["healthy"] is False


def test_launchagent_snapshot_uses_its_absolute_ccs_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProfileStore(tmp_path / "store")
    fake_ccs = tmp_path / "ccs"
    fake_ccs.write_text("#!/bin/sh\n")
    monkeypatch.setattr(service.sys, "argv", [str(fake_ccs), "service", "run"])
    monkeypatch.setattr(
        service, "_trusted_ccs_path", lambda: (_ for _ in ()).throw(AssertionError("PATH lookup used"))
    )
    seen: list[Path] = []

    class FakeTmux:
        def __init__(self, _store, *, ccs_binary):
            seen.append(ccs_binary)

        def list_sessions(self):
            return []

    monkeypatch.setattr("cc_swaper.tmux_sessions.TmuxSessions", FakeTmux)
    assert service._snapshot(store)["sessions"] == []
    assert seen == [fake_ccs]


def test_snapshot_keeps_same_project_runs_distinct_and_legacy_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProfileStore(tmp_path / "store")
    store.add_default("main")
    store.add_managed("second")
    cwd = tmp_path / "project"
    cwd.mkdir()
    first_id = "550e8400-e29b-41d4-a716-446655440000"
    second_id = "550e8400-e29b-41d4-a716-446655440001"
    store.set_background_session("a1b2c3d4e5f6", cwd, first_id, "main")
    store.set_background_session("b1c2d3e4f5a6", cwd, second_id, "second")
    store.set_last_session(cwd, second_id)
    fake_ccs = tmp_path / "ccs"
    fake_ccs.write_text("#!/bin/sh\n")
    monkeypatch.setattr(service, "_trusted_ccs_path", lambda: fake_ccs)

    class FakeTmux:
        def __init__(self, _store, *, ccs_binary):
            assert ccs_binary == fake_ccs

        def list_sessions(self):
            return [
                {"name": "first", "cwd": str(cwd), "run_id": "a1b2c3d4e5f6", "session_id": first_id,
                 "initial_profile": "main", "attached": False, "dead": False},
                {"name": "second", "cwd": str(cwd), "run_id": "b1c2d3e4f5a6", "session_id": second_id,
                 "initial_profile": "main", "attached": True, "dead": False},
                {"name": "legacy", "cwd": str(cwd), "run_id": None, "session_id": None,
                 "initial_profile": "main", "attached": False, "dead": False},
            ]

    monkeypatch.setattr("cc_swaper.tmux_sessions.TmuxSessions", FakeTmux)
    rows = service._snapshot(store)["sessions"]
    assert [(row["run_id"], row["session_id"], row["profile"]) for row in rows] == [
        ("a1b2c3d4e5f6", first_id, "main"),
        ("b1c2d3e4f5a6", second_id, "second"),
        (None, None, "main"),
    ]


def test_service_plist_persists_custom_tmux_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProfileStore(tmp_path / "store")
    fake_ccs = tmp_path / "ccs"
    fake_ccs.write_text("#!/bin/sh\n")
    monkeypatch.setattr(service, "_trusted_ccs_path", lambda: fake_ccs)
    monkeypatch.setenv("CC_SWAPER_TMUX_SOCKET", "my-private-socket")
    payload = service._desired_plist(store)
    assert payload["EnvironmentVariables"]["CC_SWAPER_TMUX_SOCKET"] == "my-private-socket"


def test_launchagent_install_updates_socket_and_restarts_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProfileStore(tmp_path / "store")
    plist = tmp_path / "LaunchAgents" / f"{service.LABEL}.plist"
    fake_ccs = tmp_path / "ccs"
    fake_ccs.write_text("#!/bin/sh\n")
    monkeypatch.setattr(service, "_plist_path", lambda: plist)
    monkeypatch.setattr(service, "_trusted_ccs_path", lambda: fake_ccs)
    loaded = False
    actions: list[str] = []

    def fake_run(args, **_kwargs):
        nonlocal loaded
        action = args[1]
        actions.append(action)
        if action == "bootstrap":
            loaded = True
        elif action == "bootout":
            loaded = False
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_loaded", lambda: loaded)
    monkeypatch.setattr(service.subprocess, "run", fake_run)
    service.install(store)
    monkeypatch.setenv("CC_SWAPER_TMUX_SOCKET", "alternate-socket")
    service.install(store)
    updated = plistlib.loads(plist.read_bytes())
    assert updated["EnvironmentVariables"]["CC_SWAPER_TMUX_SOCKET"] == "alternate-socket"
    assert actions == ["bootstrap", "bootout", "bootstrap"]
    service.uninstall(store)
