from __future__ import annotations

import io
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


def test_switch_without_name_offers_numbered_profiles_and_keeps_named_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    store.add_managed("second")

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    output = Tty()
    monkeypatch.setattr(cli.sys, "stdin", Tty("2\n"))
    monkeypatch.setattr(cli.sys, "stdout", output)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli, "_background_session",
        lambda _store, **kwargs: calls.append(kwargs) or 0,
    )

    assert cli.main(["switch"]) == 0
    assert "1. main (đang chọn)" in output.getvalue()
    assert "2. second" in output.getvalue()
    assert calls[0]["profile_name"] == "second"
    # The detached child owns selection once its Claude session starts.
    assert ProfileStore().selected().name == "main"

    assert cli.main(["switch", "main"]) == 0
    assert calls[1]["profile_name"] == "main"
    assert ProfileStore().selected().name == "main"


def test_background_switch_does_not_overwrite_newer_child_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    store.add_managed("second")
    store.add_managed("third")

    def fake_background(inner_store, **_kwargs):
        # Model a detached child that has already moved to the next profile.
        inner_store.select("third")
        return 0

    monkeypatch.setattr(cli, "_background_session", fake_background)
    assert cli.main(["switch", "second"]) == 0
    assert ProfileStore().selected().name == "third"


def test_failed_foreground_switch_does_not_undo_concurrent_same_target_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    store.add_managed("second")

    def fake_session(inner_store, **_kwargs):
        # This switch fails before its own launch, while another process
        # independently chooses the same target name.
        ProfileStore().select("second")
        raise RuntimeError("launch failed")

    monkeypatch.setattr(cli, "_run_session", fake_session)
    assert cli.main(["switch", "--foreground", "second"]) == 2
    assert ProfileStore().selected().name == "second"


def test_switch_cancel_or_failed_launch_keeps_existing_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    store.add_managed("second")

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", Tty("0\n"))
    monkeypatch.setattr(cli.sys, "stdout", Tty())
    monkeypatch.setattr(
        cli, "_background_session",
        lambda _store, **_kwargs: (_ for _ in ()).throw(RuntimeError("session exists")),
    )
    assert cli.main(["switch"]) == 0
    assert ProfileStore().selected().name == "main"
    assert cli.main(["switch", "second"]) == 2
    assert ProfileStore().selected().name == "main"


@pytest.mark.parametrize("command", ["switch", "use"])
def test_profile_picker_requires_tty_without_a_name(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    store.add_managed("second")
    assert cli.main([command]) == 2
    assert f"ccs {command} <profile>" in capsys.readouterr().err
    assert ProfileStore().selected().name == "main"


def test_use_without_name_selects_from_the_same_menu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    store.add_managed("second")

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", Tty("2\n"))
    monkeypatch.setattr(cli.sys, "stdout", Tty())
    assert cli.main(["use"]) == 0
    assert ProfileStore().selected().name == "second"


def test_picker_accepts_numeric_profile_name_with_at_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    store.add_managed("2")

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    output = Tty()
    monkeypatch.setattr(cli.sys, "stdin", Tty("@2\n"))
    monkeypatch.setattr(cli.sys, "stdout", output)
    assert cli.main(["use"]) == 0
    assert "2. @2" in output.getvalue()
    assert ProfileStore().selected().name == "2"
