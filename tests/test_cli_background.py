from __future__ import annotations

import io
from contextlib import contextmanager
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


def test_foreground_run_rechecks_profile_after_removal_wins_start_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    store.add_managed("second")

    @contextmanager
    def removal_wins(_store, name, *, exclusive):
        assert name == "second" and not exclusive
        store.remove_managed("second")
        yield

    monkeypatch.setattr(cli, "_profile_lock", removal_wins)
    with pytest.raises(KeyError):
        cli._run_session(
            store, resume=False, explicit_session=None, profile_name="second",
            no_auto=False, passthrough=[],
        )
    assert not (store.home / "sessions.json").exists()
    assert not (store.home / "background-sessions.json").exists()


def test_forged_managed_run_id_outside_ccs_tmux_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    monkeypatch.setenv("CC_SWAPER_RUN_ID", "a1b2c3d4e5f6")
    monkeypatch.delenv("TMUX", raising=False)
    store = ProfileStore()
    store.add_default("main")
    monkeypatch.setattr(cli, "claude_binary", lambda: "/trusted/claude")
    monkeypatch.setattr(cli, "_require_login", lambda _profile, _binary: None)
    assert cli.main(["run", "--foreground"]) == 2
    assert "only valid inside its cc-swaper tmux socket" in capsys.readouterr().err
    assert not (store.home / "background-sessions.json").exists()


@pytest.mark.parametrize("command", ["run", "resume"])
def test_explicit_attach_requires_tty_before_session_creation(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    assert cli.main([command, "--attach"]) == 2
    assert "requires an interactive terminal" in capsys.readouterr().err
    assert not (store.home / "sessions.json").exists()
    assert not (store.home / "background-sessions.json").exists()


def test_conversation_lock_is_global_to_a_transcript_id(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "store")
    first = "550e8400-e29b-41d4-a716-446655440000"
    second = "550e8400-e29b-41d4-a716-446655440001"
    with cli._conversation_lock(store, first):
        with pytest.raises(RuntimeError, match="already running"):
            with cli._conversation_lock(store, first):
                pass
        with cli._conversation_lock(store, second):
            pass


def test_attach_and_stop_target_current_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    project = tmp_path / "project"
    project.mkdir()
    actions: list[tuple[str, Path, str]] = []
    name = "ccs-project-legacy"

    class FakeTmux:
        def __init__(self, _store):
            pass

        def list_sessions_for_cwd(self, cwd):
            return [{"name": name, "run_id": None, "initial_profile": "main", "dead": False}]

        def attach(self, cwd, session_name=None):
            actions.append(("attach", cwd, session_name))
            return 0

        def stop(self, cwd, session_name=None):
            actions.append(("stop", cwd, session_name))
            return 0

    monkeypatch.setattr("cc_swaper.tmux_sessions.TmuxSessions", FakeTmux)
    assert cli.main(["attach", "--project", str(project)]) == 0
    assert cli.main(["stop", "--project", str(project)]) == 0
    assert actions == [("attach", project, name), ("stop", project, name)]


def test_attach_picker_targets_only_chosen_same_project_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    actions: list[tuple[str, str]] = []
    sessions = [
        {"name": "ccs-project-a1b2c3d4e5f6", "run_id": "a1b2c3d4e5f6", "initial_profile": "main", "dead": False},
        {"name": "ccs-project-b1c2d3e4f5a6", "run_id": "b1c2d3e4f5a6", "initial_profile": "main", "dead": False},
    ]

    class FakeTmux:
        def __init__(self, _store):
            pass

        def list_sessions_for_cwd(self, _cwd):
            return sessions

        def attach(self, _cwd, session_name=None):
            actions.append(("attach", session_name))
            return 0

        def stop(self, _cwd, session_name=None):
            actions.append(("stop", session_name))
            return 0

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("cc_swaper.tmux_sessions.TmuxSessions", FakeTmux)
    menu = Tty()
    monkeypatch.setattr(cli.sys, "stdin", Tty("2\n"))
    monkeypatch.setattr(cli.sys, "stdout", menu)
    assert cli.main(["attach"]) == 0
    assert "Choose a session to attach" in menu.getvalue()
    assert actions == [("attach", sessions[1]["name"])]

    assert cli.main(["stop", "--session", "a1b2c3d4e5f6"]) == 0
    assert actions[-1] == ("stop", sessions[0]["name"])


@pytest.mark.parametrize("command", ["attach", "stop"])
def test_ambiguous_session_requires_selector_when_not_interactive(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    class FakeTmux:
        def __init__(self, _store):
            pass

        def list_sessions_for_cwd(self, _cwd):
            return [
                {"name": "first", "run_id": "a1b2c3d4e5f6", "dead": False},
                {"name": "second", "run_id": "b1c2d3e4f5a6", "dead": False},
            ]

        def attach(self, *_args, **_kwargs):
            raise AssertionError("ambiguous session was attached")

        def stop(self, *_args, **_kwargs):
            raise AssertionError("ambiguous session was stopped")

    monkeypatch.setattr("cc_swaper.tmux_sessions.TmuxSessions", FakeTmux)
    assert cli.main([command]) == 2
    assert f"ccs {command} --session <run-id>" in capsys.readouterr().err


def test_resume_attach_reuses_active_conversation_instead_of_starting_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    session_id = "550e8400-e29b-41d4-a716-446655440000"
    transcript = tmp_path / ".claude" / "projects" / "test-project" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()
    store.set_last_session(project, session_id, profile_name="main", transcript_path=transcript)
    monkeypatch.setattr(cli, "claude_binary", lambda: "/trusted/claude")
    monkeypatch.setattr(cli, "_require_login", lambda _profile, _binary: None)
    actions: list[str] = []

    class FakeTmux:
        def __init__(self, _store):
            pass

        def list_sessions_for_cwd(self, _cwd):
            return [{
                "name": "ccs-project-a1b2c3d4e5f6", "run_id": "a1b2c3d4e5f6",
                "session_id": session_id, "dead": False,
            }]

        def attach(self, _cwd, session_name=None):
            actions.append(session_name)
            return 0

        def start(self, *_args, **_kwargs):
            raise AssertionError("resume opened a duplicate writer")

    monkeypatch.setattr("cc_swaper.tmux_sessions.TmuxSessions", FakeTmux)
    assert cli._background_session(
        store, mode="resume", profile_name="main", claude_args=[],
        no_auto=False, attach=True,
    ) == 0
    assert actions == ["ccs-project-a1b2c3d4e5f6"]


def test_resume_refuses_unknown_legacy_writer_after_new_run_changes_last_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    old_id = "550e8400-e29b-41d4-a716-446655440000"
    new_id = "550e8400-e29b-41d4-a716-446655440001"
    root = tmp_path / ".claude" / "projects" / "test-project"
    root.mkdir(parents=True)
    for session_id in (old_id, new_id):
        (root / f"{session_id}.jsonl").touch()
    store.set_last_session(project, new_id, profile_name="main", transcript_path=root / f"{new_id}.jsonl")
    monkeypatch.setattr(cli, "claude_binary", lambda: "/trusted/claude")
    monkeypatch.setattr(cli, "_require_login", lambda _profile, _binary: None)

    class FakeTmux:
        def __init__(self, _store):
            pass

        def list_sessions_for_cwd(self, _cwd):
            return [
                {"name": "ccs-project-legacy", "run_id": None, "session_id": None, "dead": False},
                {"name": "ccs-project-a1b2c3d4e5f6", "run_id": "a1b2c3d4e5f6",
                 "session_id": new_id, "dead": False},
            ]

        def attach(self, *_args, **_kwargs):
            raise AssertionError("unverified legacy transcript was attached")

        def start(self, *_args, **_kwargs):
            raise AssertionError("duplicate legacy writer was started")

    monkeypatch.setattr("cc_swaper.tmux_sessions.TmuxSessions", FakeTmux)
    with pytest.raises(RuntimeError, match="legacy background session is active"):
        cli._background_session(
            store, mode="resume", profile_name="main", claude_args=[],
            source_session_id=old_id, no_auto=False, attach=True,
        )


def test_resume_refuses_sole_unknown_legacy_session_without_last_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    attached: list[str] = []

    class FakeTmux:
        def __init__(self, _store):
            pass

        def list_sessions_for_cwd(self, _cwd):
            return [{"name": "ccs-project-legacy", "run_id": None,
                     "session_id": None, "dead": False}]

        def attach(self, _cwd, session_name=None):
            attached.append(session_name)
            return 0

    monkeypatch.setattr("cc_swaper.tmux_sessions.TmuxSessions", FakeTmux)
    with pytest.raises(RuntimeError, match="conversation ID is unknown"):
        cli._background_session(
            store, mode="resume", profile_name="main", claude_args=[],
            no_auto=False, attach=True,
        )
    assert attached == []


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
    assert "1. main (selected)" in output.getvalue()
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
