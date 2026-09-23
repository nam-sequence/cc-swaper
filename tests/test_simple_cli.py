from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import uuid
from pathlib import Path

import pytest

from cc_swaper import cli, service, shared_mcp_plugins, shared_settings, tmux_sessions
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


def _switch_in_pty(
    store: ProfileStore,
    keys: bytes,
    auth_marker: Path,
    *,
    interrupt: bool = False,
    terminal_width: int = 80,
) -> tuple[int, str, list[object], list[object]]:
    """Run the real interactive command on a PTY and return its terminal state."""

    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(
        slave_fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 24, terminal_width, 0, 0),
    )
    original_terminal = termios.tcgetattr(slave_fd)
    source_dir = str(Path(__file__).resolve().parents[1] / "src")
    env = {
        "HOME": str(store.home.parent),
        "CC_SWAPER_HOME": str(store.home),
        "CC_SWAPER_TEST_AUTH_MARKER": str(auth_marker),
        "PYTHONPATH": source_dir,
        "TERM": "xterm-256color",
    }
    code = """
import os
from pathlib import Path
from cc_swaper import cli

cli.claude_binary = lambda: '/fake/claude'

def fake_auth_status(*_args):
    Path(os.environ['CC_SWAPER_TEST_AUTH_MARKER']).write_text('checked')
    return True, 'claude.ai'

cli.auth_status = fake_auth_status
raise SystemExit(cli.main(['switch']))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
    )
    output = bytearray()
    try:
        deadline = time.monotonic() + 5
        # Wait for the picker to enter character mode before sending keys, so
        # the PTY's canonical line discipline cannot consume or rewrite them.
        while termios.tcgetattr(slave_fd)[3] & termios.ICANON:
            if process.poll() is not None:
                break
            if time.monotonic() >= deadline:
                pytest.fail("switch picker did not enter character mode")
            readable, _, _ = select.select([master_fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(master_fd, 4096))
                except OSError:
                    break

        if process.poll() is None:
            menu_mode = termios.tcgetattr(slave_fd)
            assert not (menu_mode[3] & termios.ICANON), (
                "switch picker exited or remained in canonical terminal mode; "
                f"output so far: {output.decode(errors='replace')}"
            )
            assert not (menu_mode[3] & termios.ISIG), (
                "switch picker left terminal-generated signals enabled"
            )
            if interrupt:
                process.send_signal(signal.SIGINT)
            else:
                os.write(master_fd, keys)

        deadline = time.monotonic() + 5
        while process.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail(
                    "switch picker did not finish after key input; "
                    f"output so far: {output.decode(errors='replace')}"
                )
            readable, _, _ = select.select([master_fd], [], [], 0.05)
            if readable:
                try:
                    output.extend(os.read(master_fd, 4096))
                except OSError:
                    break
        return_code = process.wait(timeout=1)
        # Capture the final status message after the process has restored its
        # terminal and exited.
        readable, _, _ = select.select([master_fd], [], [], 0.05)
        if readable:
            try:
                output.extend(os.read(master_fd, 4096))
            except OSError:
                pass
        restored_terminal = termios.tcgetattr(slave_fd)
        return (
            return_code,
            output.decode(errors="replace"),
            original_terminal,
            restored_terminal,
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master_fd)
        os.close(slave_fd)


def _assert_terminal_restored(
    original_terminal: list[object], restored_terminal: list[object]
) -> None:
    assert restored_terminal[:3] == original_terminal[:3]
    # The PTY kernel sets PENDIN after character-mode input returns to canonical
    # mode, even when the program restores the original termios attributes.
    pending_input_flag = getattr(termios, "PENDIN", 0)
    assert restored_terminal[3] & ~pending_input_flag == (
        original_terminal[3] & ~pending_input_flag
    )
    assert restored_terminal[4:] == original_terminal[4:]


def test_switch_picker_down_selects_profile_and_restores_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    auth_marker = tmp_path / "auth-checked"

    return_code, output, original_terminal, restored_terminal = _switch_in_pty(
        store, b"\x1b[B\r", auth_marker
    )

    assert return_code == 0
    assert "Selected 'work'." in output
    assert ProfileStore(store.home).selected().name == "work"
    assert auth_marker.read_text() == "checked"
    _assert_terminal_restored(original_terminal, restored_terminal)


@pytest.mark.parametrize(
    "up_sequence",
    [b"\x1b[A", b"\x1bOA"],
    ids=["csi", "ss3"],
)
def test_switch_picker_up_selects_profile_and_restores_terminal(
    up_sequence: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.select("work")
    auth_marker = tmp_path / "auth-checked"

    return_code, output, original_terminal, restored_terminal = _switch_in_pty(
        store, up_sequence + b"\r", auth_marker
    )

    assert return_code == 0
    assert "Selected 'main'." in output
    assert ProfileStore(store.home).selected().name == "main"
    assert auth_marker.read_text() == "checked"
    _assert_terminal_restored(original_terminal, restored_terminal)


def test_switch_picker_enter_keeps_the_initially_selected_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.select("work")
    auth_marker = tmp_path / "auth-checked"

    return_code, output, original_terminal, restored_terminal = _switch_in_pty(
        store, b"\r", auth_marker
    )

    assert return_code == 0
    assert "Selected 'work'." in output
    assert ProfileStore(store.home).selected().name == "work"
    assert auth_marker.read_text() == "checked"
    _assert_terminal_restored(original_terminal, restored_terminal)


def test_switch_picker_escape_cancels_without_auth_or_selection_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    auth_marker = tmp_path / "auth-checked"

    return_code, output, original_terminal, restored_terminal = _switch_in_pty(
        store, b"\x1b", auth_marker
    )

    assert return_code == 0
    assert "Account selection canceled." in output
    assert ProfileStore(store.home).selected().name == "main"
    assert not auth_marker.exists()
    _assert_terminal_restored(original_terminal, restored_terminal)


def test_switch_picker_sigint_cancels_and_restores_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    auth_marker = tmp_path / "auth-checked"

    return_code, output, original_terminal, restored_terminal = _switch_in_pty(
        store, b"", auth_marker, interrupt=True
    )

    assert return_code == 0
    assert "Account selection canceled." in output
    assert ProfileStore(store.home).selected().name == "main"
    assert not auth_marker.exists()
    _assert_terminal_restored(original_terminal, restored_terminal)


def test_switch_picker_ctrl_z_cancels_and_restores_terminal_and_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    auth_marker = tmp_path / "auth-checked"

    return_code, output, original_terminal, restored_terminal = _switch_in_pty(
        store, b"\x1a", auth_marker
    )

    assert return_code == 0
    assert "Account selection canceled." in output
    assert "\x1b[?25l" in output
    assert "\x1b[?25h" in output
    assert ProfileStore(store.home).selected().name == "main"
    assert not auth_marker.exists()
    _assert_terminal_restored(original_terminal, restored_terminal)


def test_switch_picker_clips_long_rows_on_narrow_terminal_and_selects_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    long_name = "long_" + "x" * 100
    store.add_managed(long_name)
    auth_marker = tmp_path / "auth-checked"
    terminal_width = 40

    return_code, output, original_terminal, restored_terminal = _switch_in_pty(
        store,
        b"\x1b[B\x1b[B\r",
        auth_marker,
        terminal_width=terminal_width,
    )

    raw_rows = re.findall(r"\r\x1b\[2K([^\r\n]*)", output)
    visible_rows = [
        re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", row) for row in raw_rows
    ]
    assert return_code == 0
    assert "Selected '" + long_name + "'." in output
    assert ProfileStore(store.home).selected().name == long_name
    assert auth_marker.read_text() == "checked"
    assert len(visible_rows) >= 9  # initial render plus two arrow redraws
    assert any(long_name[:12] in row for row in visible_rows)
    assert all(len(row) <= terminal_width for row in visible_rows)
    _assert_terminal_restored(original_terminal, restored_terminal)


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
    monkeypatch.setattr(shared_settings, "prepare_shared_settings", lambda *_args: ([], {}))
    monkeypatch.setattr(shared_mcp_plugins, "prepare_shared_mcp_plugins", lambda *_args: ([], {}))
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


def test_native_applies_shared_overlays_only_to_normal_managed_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path, monkeypatch)
    store.select("work")
    monkeypatch.setattr(cli, "claude_binary", lambda: "/fake/claude")
    calls: list[str] = []
    launches: list[tuple[list[str], dict[str, str]]] = []

    def settings_plan(_store: ProfileStore, profile) -> tuple[list[str], dict[str, str]]:
        assert profile.name == "work"
        calls.append("settings")
        return ["--settings", "/private/settings.json", "--add-dir", "/private/view"], {
            "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1"
        }

    def mcp_plan(_store: ProfileStore, profile, cwd: Path) -> tuple[list[str], dict[str, str]]:
        assert profile.name == "work" and cwd == Path.cwd()
        calls.append("mcp")
        return ["--mcp-config", "/private/mcp.json"], {
            "CLAUDE_CODE_PLUGIN_SEED_DIR": "/private/plugins"
        }

    monkeypatch.setattr(shared_settings, "prepare_shared_settings", settings_plan)
    monkeypatch.setattr(shared_mcp_plugins, "prepare_shared_mcp_plugins", mcp_plan)
    monkeypatch.setattr(
        cli, "run_passthrough",
        lambda _binary, args, env: launches.append((args, env)) or 0,
    )

    assert cli.main(["native", "--", "--model", "opus"]) == 0
    assert calls == ["settings", "mcp"]
    args, env = launches[-1]
    assert args == [
        "--settings", "/private/settings.json", "--add-dir", "/private/view",
        "--mcp-config", "/private/mcp.json", "--model", "opus",
    ]
    assert env["CLAUDE_CONFIG_DIR"] == str(store.get("work").config_dir)
    assert env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"] == "1"
    assert env["CLAUDE_CODE_PLUGIN_SEED_DIR"] == "/private/plugins"

    calls.clear()
    for admin_command in (
        "auth", "auto-mode", "daemon", "mcp", "plugin", "remote-control",
        "self-hosted-runner",
    ):
        assert cli.main(["native", "--", admin_command, "status"]) == 0
        assert calls == []
        assert launches[-1][0] == [admin_command, "status"]
    for leading_flag in (
        "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"
    ):
        assert cli.main(["native", "--", leading_flag, "daemon", "status"]) == 0
        assert calls == []
        assert launches[-1][0] == [leading_flag, "daemon", "status"]
    assert cli.main(["native", "--", "--bare"]) == 0
    assert calls == []
    assert launches[-1][0] == ["--bare"]

    assert cli.main(["native", "--", "--bg"]) == 2
    assert "background Claude sessions are unavailable" in capsys.readouterr().err
    assert calls == []
    assert launches[-1][0] == ["--bare"]

    store.select("main")
    assert cli.main(["native", "--", "--model", "sonnet"]) == 0
    assert calls == []
    assert launches[-1][0] == ["--model", "sonnet"]
    assert "CLAUDE_CONFIG_DIR" not in launches[-1][1]
    assert cli.main(["native", "--", "--bg"]) == 0
    assert launches[-1][0] == ["--bg"]


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
