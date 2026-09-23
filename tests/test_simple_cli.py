from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from cc_swaper import cli, service, tmux_sessions
from cc_swaper.profiles import ProfileStore


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProfileStore:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    store.add_managed("work")
    return store


def test_switch_persists_account_without_starting_a_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    checks: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "claude_binary", lambda: "/fake/claude")

    def auth_status(profile, binary):
        checks.append((profile.name, binary))
        return True, "claude.ai"

    monkeypatch.setattr(cli, "auth_status", auth_status)
    monkeypatch.setattr(cli, "run_passthrough", lambda *_args: pytest.fail("launched Claude"))
    monkeypatch.setattr(cli, "_native", lambda *_args: pytest.fail("started a native session"))
    monkeypatch.setattr(
        cli, "_legacy_session", lambda *_args: pytest.fail("started a tmux session")
    )

    assert cli.main(["switch", "work"]) == 0

    assert checks == [("work", "/fake/claude")]
    assert ProfileStore(store.home).selected().name == "work"
    assert ProfileStore(store.home).background_sessions() == {}


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_switch_picker_cancellation_keeps_the_current_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    terminal_in = _TTY("0\n")
    terminal_out = _TTY()
    monkeypatch.setattr(cli.sys, "stdin", terminal_in)
    monkeypatch.setattr(cli.sys, "stdout", terminal_out)
    monkeypatch.setattr(
        cli, "auth_status", lambda *_args: pytest.fail("checked login after cancellation")
    )

    assert cli.main(["switch"]) == 0

    assert "Account selection canceled." in terminal_out.getvalue()
    assert ProfileStore(store.home).selected().name == "main"


def test_switch_refuses_a_logged_out_profile_without_changing_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "claude_binary", lambda: "/fake/claude")
    monkeypatch.setattr(cli, "auth_status", lambda *_args: (False, "none"))

    assert cli.main(["switch", "work"]) == 2

    assert ProfileStore(store.home).selected().name == "main"
    assert "profile 'work' is not signed in" in capsys.readouterr().err


def test_list_escapes_control_characters_in_identity_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "claude_binary", lambda: "/fake/claude")
    monkeypatch.setattr(cli, "auth_status", lambda *_args: (True, "claude.ai"))
    monkeypatch.setattr(
        cli, "auth_details",
        lambda *_args: {"email": "me\x1b[31m@example.com", "orgName": "org\nspoof"},
    )

    assert cli.main(["list", "--show-identity"]) == 0

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "me?[31m@example.com" in output
    assert "org?spoof" in output


def test_native_runs_selected_profile_and_holds_lock_for_claude_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.select("work")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-claude")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setattr(cli, "claude_binary", lambda: "/fake/claude")
    observed: dict[str, object] = {}

    def fake_claude(binary: str, args: list[str], env: dict[str, str]) -> int:
        observed.update(binary=binary, args=args, env=env)
        # An exclusive lock must conflict while native Claude is still running.
        with pytest.raises(RuntimeError, match="currently in use"):
            with cli._profile_lock(ProfileStore(store.home), "work", exclusive=True):
                pass
        return 17

    monkeypatch.setattr(cli, "run_passthrough", fake_claude)

    assert cli.main(["native", "--", "--model", "opus", "hello"]) == 17

    assert observed["binary"] == "/fake/claude"
    assert observed["args"] == ["--model", "opus", "hello"]
    env = observed["env"]
    assert isinstance(env, dict)
    assert env["CLAUDE_CONFIG_DIR"] == str(store.get("work").config_dir)
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    # The lock is released after the direct Claude process returns.
    with cli._profile_lock(ProfileStore(store.home), "work", exclusive=True):
        pass


@pytest.mark.parametrize("action", ["attach", "stop"])
def test_legacy_commands_route_to_existing_tmux_sessions(
    action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    run_id = "a1b2c3d4e5f6"
    store.set_background_session(
        run_id,
        tmp_path,
        str(uuid.UUID("12345678-1234-5678-1234-567812345678")),
        "main",
    )
    calls: list[tuple[str, Path, str]] = []

    class ExistingSessions:
        def __init__(self, actual_store: ProfileStore) -> None:
            assert actual_store.home == store.home

        def list_sessions_for_cwd(self, cwd: Path) -> list[dict[str, object]]:
            return [{
                "run_id": run_id,
                "name": "old-project-session",
                "dead": action == "stop",
                "attached": False,
            }]

        def attach(self, cwd: Path, *, session_name: str) -> int:
            calls.append(("attach", cwd, session_name))
            return 0

        def stop(self, cwd: Path, *, session_name: str) -> int:
            calls.append(("stop", cwd, session_name))
            return 0

    monkeypatch.setattr(tmux_sessions, "TmuxSessions", ExistingSessions)

    assert cli.main([action, "--project", str(tmp_path), "--session", run_id]) == 0

    assert calls == [(action, tmp_path.resolve(), "old-project-session")]
    if action == "stop":
        assert ProfileStore(store.home).background_session(run_id) is None
    else:
        assert ProfileStore(store.home).background_session(run_id) is not None


def test_setup_uninstalls_old_service_and_installs_shell_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path, monkeypatch)
    installed_path = tmp_path / ".zshrc"
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(
        service,
        "uninstall",
        lambda actual_store: calls.append(("service-uninstall", actual_store.home)),
    )
    monkeypatch.setattr(
        cli.shell_integration,
        "install",
        lambda actual_store: calls.append(("shell-install", actual_store.home))
        or installed_path,
    )

    assert cli.main(["setup"]) == 0

    assert calls == [
        ("service-uninstall", store.home),
        ("shell-install", store.home),
    ]
    assert str(installed_path) in capsys.readouterr().out


def test_native_subprocess_uses_selected_profile_without_creating_session_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.select("work")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude = fake_bin / "claude"
    claude.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({'args': sys.argv[1:], "
        "'config': os.environ.get('CLAUDE_CONFIG_DIR'), "
        "'api_key': os.environ.get('ANTHROPIC_API_KEY')}))\n"
        "sys.exit(17)\n",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["CC_SWAPER_HOME"] = str(store.home)
    env["CC_SWAPER_CLAUDE_BIN"] = str(claude)
    env["ANTHROPIC_API_KEY"] = "must-not-reach-claude"
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    result = subprocess.run(
        [sys.executable, "-m", "cc_swaper", "native", "--", "--model", "opus", "hello"],
        env=env, capture_output=True, text=True, timeout=10, check=False,
    )

    assert result.returncode == 17, result.stderr
    assert json.loads(result.stdout) == {
        "args": ["--model", "opus", "hello"],
        "config": str(store.get("work").config_dir),
        "api_key": None,
    }
    assert not (store.home / "sessions.json").exists()
    assert not (store.home / "background-sessions.json").exists()
