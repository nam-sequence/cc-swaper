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


def test_start_pins_profile_binary_quotes_args_and_rejects_duplicate(
    manager: tuple[TmuxSessions, FakeTmux, Path],
) -> None:
    sessions, fake, root = manager
    project = root / "project with spaces; $HOME"
    project.mkdir()

    name = sessions.start(
        project,
        "work",
        "run",
        ["--model", "sonnet; echo unsafe", "--label", "a b"],
        no_auto=True,
    )
    command = fake.sessions[name]["command"]
    assert isinstance(command, str)
    parsed = shlex.split(command)
    profile = sessions.store.get("work")
    assert parsed[:10] == [
        "/usr/bin/env",
        f"CC_SWAPER_CLAUDE_BIN={root / 'bin' / 'claude'}",
        f"CC_SWAPER_HOME={sessions.store.home}",
        "CC_SWAPER_TMUX_SOCKET=test-cc-swaper",
        f"CLAUDE_CONFIG_DIR={profile.config_dir}",
        str(root / "bin" / "ccs"),
        "run",
        "--foreground",
        "--profile",
        "work",
    ]
    assert parsed[10:] == [
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
    }
    assert fake.calls[0][1:3] == ["-L", "test-cc-swaper"]
    assert not any(key.startswith("ANTHROPIC_") for key in fake.environments[0])
    assert sessions.exists(project)

    with pytest.raises(RuntimeError, match="already exists"):
        sessions.start(project, "work", "run", [])


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
        [session_id],
        detach=False,
        no_auto=True,
    )
    command = shlex.split(fake.sessions[name]["command"])
    ccs_index = command.index(str(root / "bin" / "ccs"))
    assert command[ccs_index : ccs_index + 8] == [
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


def test_start_replaces_a_retained_dead_session(
    manager: tuple[TmuxSessions, FakeTmux, Path],
) -> None:
    sessions, fake, root = manager
    project = root / "restart"
    project.mkdir()
    first = sessions.start(project, "main", "run", [])
    fake.sessions[first]["dead"] = True

    second = sessions.start(project, "main", "run", [])

    assert second == first
    assert len([call for call in fake.calls if call[3] == "new-session"]) == 2


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

    with pytest.raises(RuntimeError, match="invalid cc-swaper metadata"):
        sessions.start(project, "main", "run", [])
    with pytest.raises(RuntimeError, match="invalid cc-swaper metadata"):
        sessions.attach(project)
    with pytest.raises(RuntimeError, match="invalid cc-swaper metadata"):
        sessions.stop(project)
    assert name in fake.sessions
