from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_swaper.profiles import ProfileStore
from cc_swaper.tmux_sessions import TmuxSessions, session_name


class FakeTmux:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.sessions: dict[str, dict[str, object]] = {}

    def __call__(self, argv: list[str], **kwargs: object) -> SimpleNamespace:
        self.calls.append(argv)
        self.environments.append(dict(kwargs.get("env", {})))
        operation = argv[3]

        def target_name() -> str:
            target = argv[argv.index("-t") + 1]
            if target.startswith("="):
                target = target[1:]
            return target.removesuffix(":")

        if operation == "has-session":
            name = target_name()
            return self._result(returncode=0 if name in self.sessions else 1)
        if operation == "new-session":
            name = argv[argv.index("-s") + 1]
            if name in self.sessions:
                return self._result(returncode=1, stderr="duplicate session")
            cwd = argv[argv.index("-c") + 1]
            self.sessions[name] = {
                "cwd": cwd,
                "command": argv[-1],
                "options": {},
                "attached": 0,
                "dead": False,
            }
            return self._result()
        if operation == "set-option":
            name = target_name()
            option_index = argv.index("-t") + 2
            self.sessions[name]["options"][argv[option_index]] = argv[option_index + 1]
            return self._result()
        if operation == "list-sessions":
            lines = []
            for name, state in self.sessions.items():
                options = state["options"]
                lines.append(
                    "\t".join(
                        [
                            name,
                            str(state["attached"]),
                            str(options.get("@ccs_cwd", "")),
                            str(options.get("@ccs_initial_profile", "")),
                            str(options.get("@ccs_run_id", "")),
                            str(options.get("@ccs_session_id", "")),
                        ]
                    )
                )
            if not lines:
                return self._result(returncode=1, stderr="no server running")
            return self._result(stdout="\n".join(lines) + "\n")
        if operation == "list-panes":
            name = target_name()
            state = self.sessions[name]
            return self._result(
                stdout=f"{'1' if state['dead'] else '0'}\t{state['cwd']}\n"
            )
        if operation in {"attach-session", "switch-client"}:
            name = target_name()
            if name not in self.sessions:
                return self._result(returncode=1, stderr="session not found")
            self.sessions[name]["attached"] = 1
            return self._result()
        if operation == "kill-session":
            name = target_name()
            if name not in self.sessions:
                return self._result(returncode=1, stderr="session not found")
            del self.sessions[name]
            return self._result()
        raise AssertionError(f"unexpected tmux operation: {operation}")

    @staticmethod
    def _result(
        *, returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> SimpleNamespace:
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TmuxSessions, FakeTmux, Path]:
    store = ProfileStore(tmp_path / "store")
    store.add_default("main")
    store.add_managed("work")
    ccs = _executable(tmp_path / "bin" / "ccs")
    tmux = _executable(tmp_path / "bin" / "tmux")
    claude = _executable(tmp_path / "bin" / "claude")
    fake = FakeTmux()
    monkeypatch.setenv("CC_SWAPER_CLAUDE_BIN", str(claude))
    monkeypatch.setenv("CC_SWAPER_TMUX_SOCKET", "test-cc-swaper")
    monkeypatch.setattr("cc_swaper.tmux_sessions.subprocess.run", fake)
    return TmuxSessions(store, ccs_binary=ccs, tmux_binary=tmux), fake, tmp_path


def test_session_name_uses_canonical_path_slug_and_digest(tmp_path: Path) -> None:
    project = tmp_path / "A project!"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)

    first = session_name(project)
    second = session_name(alias)

    assert first == second
    assert first.startswith("ccs-a-project-")
    assert len(first.rsplit("-", 1)[-1]) == 12


def test_start_pins_profile_binary_quotes_args_and_tags_invocation(
    manager: tuple[TmuxSessions, FakeTmux, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions, fake, root = manager
    project = root / "project with spaces; $HOME"
    project.mkdir()
    monkeypatch.setenv("CC_SWAPER_RUN_ID", "caller-controlled")
    run_session_id = "550e8400-e29b-41d4-a716-446655440000"

    name = sessions.start(
        project,
        "work",
        "run",
        ["--model", "sonnet; echo unsafe", "--label", "a b"],
        no_auto=True,
        run_session_id=run_session_id,
    )
    assert name.startswith(f"{session_name(project)}-")
    run_id = name.removeprefix(f"{session_name(project)}-")
    assert len(run_id) == 12
    assert all(character in "0123456789abcdef" for character in run_id)
    command = fake.sessions[name]["command"]
    assert isinstance(command, str)
    parsed = shlex.split(command)
    profile = sessions.store.get("work")
    assert parsed[:6] == [
        "/usr/bin/env",
        f"CC_SWAPER_CLAUDE_BIN={root / 'bin' / 'claude'}",
        f"CC_SWAPER_HOME={sessions.store.home}",
        f"CC_SWAPER_RUN_ID={run_id}",
        "CC_SWAPER_TMUX_SOCKET=test-cc-swaper",
        f"CLAUDE_CONFIG_DIR={profile.config_dir}",
    ]
    ccs_index = parsed.index(str(root / "bin" / "ccs"))
    assert parsed[ccs_index : ccs_index + 7] == [
        str(root / "bin" / "ccs"),
        "run",
        "--foreground",
        "--profile",
        "work",
        "--managed-session-id",
        parsed[ccs_index + 6],
    ]
    session_id = parsed[parsed.index("--managed-session-id") + 1]
    assert session_id == run_session_id
    assert parsed[ccs_index + 7 :] == [
        "--no-auto",
        "--",
        "--model",
        "sonnet; echo unsafe",
        "--label",
        "a b",
    ]
    assert fake.sessions[name]["options"] == {
        "remain-on-exit": "on",
        "@ccs_cwd": str(project.resolve()),
        "@ccs_initial_profile": "work",
        "@ccs_run_id": run_id,
        "@ccs_session_id": session_id,
    }
    assert fake.calls[0][1:3] == ["-L", "test-cc-swaper"]
    assert not any(key.startswith("ANTHROPIC_") for key in fake.environments[0])
    assert "CC_SWAPER_RUN_ID" not in fake.environments[0]
    assert sessions.exists(project)

    sibling = sessions.start(project, "work", "run", [])
    assert sibling != name
    listed = sessions.list_sessions_for_cwd(project)
    assert len(listed) == 2
    generated_id = next(item["session_id"] for item in listed if item["name"] == sibling)
    assert isinstance(generated_id, str) and len(generated_id) == 36


def test_resume_explicit_id_switch_order_and_attach_stop(
    manager: tuple[TmuxSessions, FakeTmux, Path],
) -> None:
    sessions, fake, root = manager
    project = root / "resume"
    project.mkdir()
    session_id = "550e8400-e29b-41d4-a716-446655440000"

    name = sessions.start(
        project,
        "work",
        "resume",
        [],
        detach=False,
        no_auto=True,
        source_session_id=session_id,
    )
    command = shlex.split(fake.sessions[name]["command"])
    ccs_index = command.index(str(root / "bin" / "ccs"))
    assert command[ccs_index : ccs_index + 7] == [
        str(root / "bin" / "ccs"),
        "resume",
        "--foreground",
        "--profile",
        "work",
        "--no-auto",
        session_id,
    ]
    assert fake.sessions[name]["attached"] == 1
    assert sessions.attach(project) == 0
    assert sessions.stop(project) == 0
    assert not sessions.exists(project)


def test_start_creates_new_invocation_without_removing_dead_sibling(
    manager: tuple[TmuxSessions, FakeTmux, Path],
) -> None:
    sessions, fake, root = manager
    project = root / "restart"
    project.mkdir()
    first = sessions.start(project, "main", "run", [])
    fake.sessions[first]["dead"] = True

    second = sessions.start(project, "main", "run", [])

    assert second != first
    assert first in fake.sessions and second in fake.sessions
    assert len(sessions.list_sessions_for_cwd(project)) == 2
    assert next(item for item in sessions.list_sessions_for_cwd(project) if item["name"] == first)["dead"]


def test_list_sessions_exposes_tags_attached_and_dead_state(
    manager: tuple[TmuxSessions, FakeTmux, Path],
) -> None:
    sessions, fake, root = manager
    project = root / "monitor me"
    project.mkdir()
    name = sessions.start(project, "main", "switch", [], no_auto=False)
    fake.sessions[name]["dead"] = True
    fake.sessions["unrelated"] = {
        "cwd": str(project), "command": "sleep 10",
        "options": {"@ccs_cwd": str(project.resolve()), "@ccs_initial_profile": "main"},
        "attached": 0, "dead": False,
    }

    listed = sessions.list_sessions()

    assert listed == [
        {
            "name": name,
            "cwd": str(project.resolve()),
            "initial_profile": "main",
            "profile": "main",
            "run_id": name.removeprefix(f"{session_name(project)}-"),
            "session_id": None,
            "attached": False,
            "dead": True,
        }
    ]
    switch_command = shlex.split(fake.sessions[name]["command"])
    ccs_index = switch_command.index(str(root / "bin" / "ccs"))
    assert switch_command[ccs_index : ccs_index + 4] == [
        str(root / "bin" / "ccs"),
        "switch",
        "--foreground",
        "main",
    ]


def test_switch_passes_source_session_id_and_token_safely(
    manager: tuple[TmuxSessions, FakeTmux, Path],
) -> None:
    sessions, fake, root = manager
    project = root / "switch"
    project.mkdir()
    session_id = "550e8400-e29b-41d4-a716-446655440000"

    name = sessions.start(
        project,
        "work",
        "switch",
        ["--model", "sonnet; echo unsafe"],
        source_session_id=session_id,
        selection_token="token with spaces; echo unsafe",
    )
    command = shlex.split(fake.sessions[name]["command"])
    ccs_index = command.index(str(root / "bin" / "ccs"))
    assert command[ccs_index : ccs_index + 8] == [
        str(root / "bin" / "ccs"),
        "switch",
        "--foreground",
        "work",
        "--source-session",
        session_id,
        "--selection-token",
        "token with spaces; echo unsafe",
    ]
    assert "sonnet; echo unsafe" in command
    assert fake.sessions[name]["options"]["@ccs_session_id"] == session_id


def test_attach_uses_attach_across_tmux_sockets_and_switch_within_same_socket(
    manager: tuple[TmuxSessions, FakeTmux, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, fake, root = manager
    project = root / "other tmux"
    project.mkdir()
    sessions.start(project, "main", "run", [])

    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,123,0")
    assert sessions.attach(project) == 0
    assert fake.calls[-1][3] == "attach-session"
    assert "TMUX" not in fake.environments[-1]

    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/test-cc-swaper,123,0")
    assert sessions.attach(project) == 0
    assert fake.calls[-1][3] == "switch-client"
    assert fake.environments[-1]["TMUX"].endswith("test-cc-swaper,123,0")


def test_predictable_name_without_ccs_metadata_is_not_attached_or_killed(
    manager: tuple[TmuxSessions, FakeTmux, Path]
) -> None:
    sessions, fake, root = manager
    project = root / "collision"
    project.mkdir()
    name = session_name(project)
    fake.sessions[name] = {
        "cwd": str(project), "command": "unrelated",
        "options": {}, "attached": 0, "dead": True,
    }

    created = sessions.start(project, "main", "run", [])
    assert created != name
    assert sessions.list_sessions_for_cwd(project) == [
        item for item in sessions.list_sessions() if item["name"] == created
    ]
    with pytest.raises(RuntimeError, match="invalid cc-swaper metadata"):
        sessions.attach(project, session_name=name)
    with pytest.raises(RuntimeError, match="invalid cc-swaper metadata"):
        sessions.stop(project, session_name=name)
    assert name in fake.sessions


def test_legacy_session_is_listed_and_implicitly_selected_when_unique(
    manager: tuple[TmuxSessions, FakeTmux, Path],
) -> None:
    sessions, fake, root = manager
    project = root / "legacy"
    project.mkdir()
    name = session_name(project)
    fake.sessions[name] = {
        "cwd": str(project.resolve()),
        "command": "ccs run --foreground",
        "options": {
            "@ccs_cwd": str(project.resolve()),
            "@ccs_initial_profile": "main",
        },
        "attached": 0,
        "dead": False,
    }

    listed = sessions.list_sessions_for_cwd(project)
    assert len(listed) == 1
    assert listed[0]["name"] == name
    assert listed[0]["run_id"] is None
    assert sessions.attach(project) == 0
    assert sessions.stop(project) == 0
    assert name not in fake.sessions


def test_ambiguous_attach_and_stop_require_exact_selector(
    manager: tuple[TmuxSessions, FakeTmux, Path],
) -> None:
    sessions, fake, root = manager
    project = root / "ambiguous"
    project.mkdir()
    first = sessions.start(project, "main", "run", [])
    second = sessions.start(project, "main", "run", [])

    with pytest.raises(RuntimeError, match="multiple background sessions"):
        sessions.attach(project)
    with pytest.raises(RuntimeError, match="multiple background sessions"):
        sessions.stop(project)

    first_record = next(item for item in sessions.list_sessions_for_cwd(project) if item["name"] == first)
    assert sessions.stop(project, run_id=first_record["run_id"]) == 0
    assert first not in fake.sessions
    assert second in fake.sessions
    assert sessions.attach(project, session_name=second) == 0
