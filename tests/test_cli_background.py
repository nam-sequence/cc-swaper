from __future__ import annotations

from pathlib import Path

import pytest

from cc_swaper import cli
from cc_swaper.profiles import ProfileStore


def test_run_defaults_to_detached_tmux_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(cli, "claude_binary", lambda: "/trusted/claude")
    monkeypatch.setattr(cli, "_require_login", lambda _profile, _binary: None)
    calls: list[tuple] = []

    class FakeTmux:
        def __init__(self, _store):
            pass

        def start(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "ccs-project-abc"

    monkeypatch.setattr("cc_swaper.tmux_sessions.TmuxSessions", FakeTmux)
    assert cli.main(["run", "--", "Fix the bug"]) == 0
    assert calls == [
        ((project, "main", "run", ["Fix the bug"]), {"detach": True, "no_auto": False})
    ]
    assert "ccs attach" in capsys.readouterr().out


def test_attach_and_stop_target_current_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    project = tmp_path / "project"
    project.mkdir()
    actions: list[tuple[str, Path]] = []

    class FakeTmux:
        def __init__(self, _store):
            pass

        def exists(self, cwd):
            return True

        def attach(self, cwd):
            actions.append(("attach", cwd))
            return 0

        def stop(self, cwd):
            actions.append(("stop", cwd))
            return 0

    monkeypatch.setattr("cc_swaper.tmux_sessions.TmuxSessions", FakeTmux)
    assert cli.main(["attach", "--project", str(project)]) == 0
    assert cli.main(["stop", "--project", str(project)]) == 0
    assert actions == [("attach", project), ("stop", project)]


def test_status_escapes_terminal_control_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    monkeypatch.setattr(cli.monitor_service, "status", lambda _store: {
        "loaded": True, "healthy": True, "error": None,
        "sessions": [{"name": "ccs-evil\x1b[31m", "cwd": "/tmp/project\nnext", "profile": "main"}],
    })
    assert cli.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "\\u001b" in output and "\\n" in output
    assert "\x1b" not in output
