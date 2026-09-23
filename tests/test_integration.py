from __future__ import annotations

import json
import fcntl
import os
import pty
import select
import signal
import shutil
import subprocess
import sys
import termios
import time
import uuid
from pathlib import Path

import pytest

from cc_swaper.profiles import ProfileStore
from cc_swaper.service import _snapshot
from cc_swaper.tmux_sessions import session_name


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys, time, uuid
from pathlib import Path

args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    if os.environ.get("FAKE_AUTH_STATUS_FAIL"):
        sys.exit(1)
    if os.environ.get("CLAUDE_CONFIG_DIR") and (Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".fake_logged_out").exists():
        print(json.dumps({"loggedIn": False, "authMethod": "none"}))
        sys.exit(1)
    profile = "secondary" if os.environ.get("CLAUDE_CONFIG_DIR") else "main"
    email = "main@example.test" if os.environ.get("FAKE_DUPLICATE_ACCOUNT") else profile + "@example.test"
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": email, "orgId": "org1"}))
    sys.exit(0)
if args[:2] == ["auth", "login"]:
    if os.environ.get("FAKE_LOGIN_LOG"):
        Path(os.environ["FAKE_LOGIN_LOG"]).write_text(os.environ.get("CLAUDE_CONFIG_DIR", "default"))
    sys.exit(0)
if args[:2] == ["auth", "logout"]:
    if os.environ.get("FAKE_LOGOUT_FAIL"):
        sys.exit(1)
    if os.environ.get("FAKE_LOGOUT_LOG"):
        Path(os.environ["FAKE_LOGOUT_LOG"]).write_text(os.environ.get("CLAUDE_CONFIG_DIR", "default"))
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        (Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".fake_logged_out").touch()
    sys.exit(0)

settings = json.loads(args[args.index("--settings") + 1])
event_file = Path(settings["hooks"]["SessionStart"][0]["hooks"][0]["args"][-1])
root = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
project = root / "projects" / "test-project"
project.mkdir(parents=True, exist_ok=True)

if "--session-id" in args:
    sid = args[args.index("--session-id") + 1]
    path = project / (sid + ".jsonl")
    path.write_text("original transcript\n")
    if os.environ.get("FAKE_PID_FILE"):
        Path(os.environ["FAKE_PID_FILE"]).write_text(str(os.getpid()))
    event_file.open("a").write(json.dumps({"type": "start", "session_id": sid, "transcript_path": str(path)}) + "\n")
    if os.environ.get("FAKE_MODE") == "normal":
        print("READY", flush=True)
        sys.exit(0)
    if os.environ.get("FAKE_MODE") == "wait_no_quota":
        print("WAITING", flush=True)
        time.sleep(30)
        sys.exit(0)
    if os.environ.get("FAKE_MODE") == "quoted_limit":
        print("Example text: You've hit your limit · resets 8am", flush=True)
        sys.exit(0)
    if os.environ.get("FAKE_MODE") == "generic_429":
        print("API Error: 429 rate_limit_error", flush=True)
        sys.exit(1)
    event_file.open("a").write(json.dumps({"type": "limit", "session_id": sid, "transcript_path": str(path)}) + "\n")
    print("You've hit your limit · resets 8am", flush=True)
    if os.environ.get("FAKE_MODE") == "late_flush":
        time.sleep(0.35)
        path.write_text("original transcript\nlate persisted\n")
    time.sleep(30)
elif "--resume" in args:
    source = Path(args[args.index("--resume") + 1])
    if not source.is_file():
        print("SOURCE MISSING", flush=True)
        sys.exit(2)
    if "--fork-session" not in args:
        print("FORK MISSING", flush=True)
        sys.exit(3)
    sid = str(uuid.uuid4())
    path = project / (sid + ".jsonl")
    path.write_text(source.read_text() + "forked\n")
    event_file.open("a").write(json.dumps({"type": "start", "session_id": sid, "transcript_path": str(path)}) + "\n")
    print("RESUMED:" + sid, flush=True)
    sys.exit(0)
else:
    print("BAD ARGS", flush=True)
    sys.exit(4)
'''


def _setup(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    fake = tmp_path / "fake-claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "CC_SWAPER_HOME": str(tmp_path / "store"),
        "CC_SWAPER_CLAUDE_BIN": str(fake),
        "ANTHROPIC_API_KEY": "must-not-reach-fake",
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    })
    for command in (["init"], ["add", "secondary", "--no-login"]):
        result = subprocess.run(
            [sys.executable, "-m", "cc_swaper", *command],
            env=env, cwd=project, capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, result.stderr
    return env, home, project


def _ccs_source_wrapper(tmp_path: Path) -> tuple[Path, Path]:
    source_dir = Path(__file__).parents[1] / "src"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ccs_wrapper = fake_bin / "ccs"
    ccs_wrapper.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(source_dir)!r})\n"
        "from cc_swaper.cli import main\n"
        "raise SystemExit(main())\n"
    )
    ccs_wrapper.chmod(0o755)
    return ccs_wrapper, fake_bin


def _run_tty(
    env: dict[str, str], project: Path, command: tuple[str, ...] = ("run", "--foreground"),
    *, terminate_on: str | None = None,
) -> tuple[int, str, list]:
    master, slave = pty.openpty()
    original_term = termios.tcgetattr(slave)
    process = subprocess.Popen(
        [sys.executable, "-m", "cc_swaper", *command],
        stdin=slave, stdout=slave, stderr=slave, env=env, cwd=project,
        start_new_session=True,
    )
    output = bytearray()
    signal_sent = False
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
                if terminate_on and not signal_sent and terminate_on in output.decode(errors="replace"):
                    process.send_signal(signal.SIGTERM)
                    signal_sent = True
            if process.poll() is not None and not ready:
                break
        code = process.wait(timeout=2)
        restored_term = termios.tcgetattr(slave)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        os.close(master)
        os.close(slave)
    return code, output.decode(errors="replace"), [original_term, restored_term]


def test_auto_switch_forks_same_conversation_and_preserves_source(tmp_path: Path) -> None:
    env, home, project = _setup(tmp_path)
    code, output, terms = _run_tty(env, project)
    assert code == 0, output
    assert "RESUMED:" in output
    before, after = terms
    assert before[:3] == after[:3]
    assert (before[3] & ~termios.PENDIN) == (after[3] & ~termios.PENDIN)
    assert before[4:] == after[4:]

    main_files = list((home / ".claude" / "projects" / "test-project").glob("*.jsonl"))
    second_files = list((tmp_path / "store" / "profiles" / "secondary" / "projects" / "test-project").glob("*.jsonl"))
    assert len(main_files) == len(second_files) == 1
    assert main_files[0].read_text() == "original transcript\n"
    assert second_files[0].read_text() == "original transcript\nforked\n"
    state = json.loads((tmp_path / "store" / "sessions.json").read_text())
    recorded = state["sessions"][str(project)]
    assert recorded == {
        "id": second_files[0].stem,
        "profile": "secondary",
        "path": str(second_files[0]),
    }
    profiles = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert profiles["selected"] == "secondary"


def test_generic_429_does_not_switch(tmp_path: Path) -> None:
    env, home, project = _setup(tmp_path)
    env["FAKE_MODE"] = "generic_429"
    code, output, _ = _run_tty(env, project)
    assert code == 1, output
    assert "RESUMED:" not in output
    assert not (tmp_path / "store" / "profiles" / "secondary" / "projects").exists()
    profiles = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert profiles["selected"] == "main"


def test_duplicate_login_is_skipped_after_limit(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    env["FAKE_DUPLICATE_ACCOUNT"] = "1"
    code, output, _ = _run_tty(env, project)
    assert code == 75, output
    assert "same Claude account" in output
    assert "RESUMED:" not in output
    profiles = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert profiles["selected"] == "main"


def test_quota_words_in_normal_output_do_not_switch(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    env["FAKE_MODE"] = "quoted_limit"
    code, output, _ = _run_tty(env, project)
    assert code == 0, output
    assert "RESUMED:" not in output
    profiles = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert profiles["selected"] == "main"


def test_manual_switch_forks_last_session(tmp_path: Path) -> None:
    env, home, project = _setup(tmp_path)
    env["FAKE_MODE"] = "normal"
    initial_code, initial_output, _ = _run_tty(env, project)
    assert initial_code == 0, initial_output
    assert "READY" in initial_output

    switch_code, switch_output, _ = _run_tty(env, project, ("switch", "--foreground", "secondary"))
    assert switch_code == 0, switch_output
    assert "RESUMED:" in switch_output
    main_file = next((home / ".claude" / "projects" / "test-project").glob("*.jsonl"))
    second_file = next((tmp_path / "store" / "profiles" / "secondary" / "projects" / "test-project").glob("*.jsonl"))
    assert main_file.read_text() == "original transcript\n"
    assert second_file.read_text() == "original transcript\nforked\n"


def test_auto_switch_waits_for_failed_turn_to_flush(tmp_path: Path) -> None:
    env, home, project = _setup(tmp_path)
    env["FAKE_MODE"] = "late_flush"
    code, output, _ = _run_tty(env, project)
    assert code == 0, output
    main_file = next((home / ".claude" / "projects" / "test-project").glob("*.jsonl"))
    second_file = next((tmp_path / "store" / "profiles" / "secondary" / "projects" / "test-project").glob("*.jsonl"))
    assert "late persisted" in main_file.read_text()
    assert "late persisted" in second_file.read_text()


def test_sigterm_restores_terminal_and_stops_child(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    env["FAKE_MODE"] = "wait_no_quota"
    code, output, terms = _run_tty(env, project, terminate_on="WAITING")
    assert code == 128 + signal.SIGTERM, output
    before, after = terms
    assert before[:3] == after[:3]
    assert (before[3] & ~termios.PENDIN) == (after[3] & ~termios.PENDIN)
    assert before[4:] == after[4:]


def test_remove_profile_logs_out_and_archives_history(tmp_path: Path) -> None:
    env, home, project = _setup(tmp_path)
    profile_dir = tmp_path / "store" / "profiles" / "secondary"
    marker = profile_dir / "keep-history.txt"
    marker.write_text("previous conversation")
    logout_log = tmp_path / "logout-target"
    env["FAKE_LOGOUT_LOG"] = str(logout_log)

    result = subprocess.run(
        [sys.executable, "-m", "cc_swaper", "remove", "secondary"],
        env=env, cwd=project, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert logout_log.read_text() == str(profile_dir)
    assert not profile_dir.exists()
    archived = list((tmp_path / "store" / "removed").glob("secondary-*"))
    assert len(archived) == 1
    assert (archived[0] / "keep-history.txt").read_text() == "previous conversation"
    assert (home / ".claude").exists() is False
    state = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert [item["name"] for item in state["profiles"]] == ["main"]
    assert state["selected"] == "main"
    readd = subprocess.run(
        [sys.executable, "-m", "cc_swaper", "add", "secondary", "--no-login"],
        env=env, cwd=project, capture_output=True, text=True, timeout=5,
    )
    assert readd.returncode == 0, readd.stderr


def test_remove_profile_purge_deletes_only_managed_data(tmp_path: Path) -> None:
    env, home, project = _setup(tmp_path)
    profile_dir = tmp_path / "store" / "profiles" / "secondary"
    (profile_dir / "secret-test-file").write_text("remove me")
    main_dir = home / ".claude"
    main_dir.mkdir()
    (main_dir / "keep.txt").write_text("keep")

    result = subprocess.run(
        [sys.executable, "-m", "cc_swaper", "remove", "secondary", "--purge-data"],
        env=env, cwd=project, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert not profile_dir.exists()
    assert (main_dir / "keep.txt").read_text() == "keep"
    assert not list((tmp_path / "store" / "removed").iterdir())
    assert not (tmp_path / "store" / "pending-removal.json").exists()


def test_remove_refuses_profile_with_active_session_lock(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    lock_dir = tmp_path / "store" / "locks"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    lock = lock_dir / "profile-secondary.lock"
    with lock.open("w+") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        result = subprocess.run(
            [sys.executable, "-m", "cc_swaper", "remove", "secondary"],
            env=env, cwd=project, capture_output=True, text=True, timeout=5,
        )
        fcntl.flock(handle, fcntl.LOCK_UN)
    assert result.returncode != 0
    assert "currently in use" in result.stderr
    assert (tmp_path / "store" / "profiles" / "secondary").is_dir()


def test_login_refuses_profile_with_active_session_lock(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    login_log = tmp_path / "login-target"
    env["FAKE_LOGIN_LOG"] = str(login_log)
    lock_dir = tmp_path / "store" / "locks"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    lock = lock_dir / "profile-secondary.lock"
    with lock.open("w+") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        result = subprocess.run(
            [sys.executable, "-m", "cc_swaper", "login", "secondary"],
            env=env, cwd=project, capture_output=True, text=True, timeout=5,
        )
        fcntl.flock(handle, fcntl.LOCK_UN)
    assert result.returncode != 0
    assert "currently in use" in result.stderr
    assert not login_log.exists()


def test_remove_cannot_touch_default_login(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    logout_log = tmp_path / "logout-target"
    env["FAKE_LOGOUT_LOG"] = str(logout_log)
    result = subprocess.run(
        [sys.executable, "-m", "cc_swaper", "remove", "main", "--purge-data"],
        env=env, cwd=project, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode != 0
    assert "default Claude profile cannot be removed" in result.stderr
    assert not logout_log.exists()
    state = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert [item["name"] for item in state["profiles"]] == ["main", "secondary"]


def test_add_waits_for_in_progress_purge_instead_of_recovering_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, fake_home, project = _setup(tmp_path)
    monkeypatch.setenv("HOME", str(fake_home))
    home = tmp_path / "store"
    store = ProfileStore(home)
    archive = home / "removed"
    archive.mkdir(mode=0o700)
    destination = archive / "secondary-20260923T000000Z-a1b2c3d4"
    store.remove_managed("secondary", archive_to=destination, purge_data=True)
    assert destination.is_dir()
    lock = home / "locks" / "profile-secondary.lock"
    lock.parent.mkdir(mode=0o700, exist_ok=True)
    with lock.open("w+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = subprocess.run(
            [sys.executable, "-m", "cc_swaper", "add", "secondary", "--no-login"],
            env=env, cwd=project, capture_output=True, text=True, timeout=5,
        )
        assert result.returncode != 0
        assert destination.is_dir()
        assert (home / "pending-removal.json").is_file()
        fcntl.flock(handle, fcntl.LOCK_UN)

    recovered = ProfileStore(home)
    assert not destination.exists()
    assert not (home / "pending-removal.json").exists()
    assert [item.name for item in recovered.all()] == ["main"]


@pytest.mark.parametrize("failure", ["FAKE_AUTH_STATUS_FAIL", "FAKE_LOGOUT_FAIL"])
def test_remove_keeps_profile_when_auth_cleanup_fails(tmp_path: Path, failure: str) -> None:
    env, _, project = _setup(tmp_path)
    env[failure] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "cc_swaper", "remove", "secondary"],
        env=env, cwd=project, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode != 0
    assert (tmp_path / "store" / "profiles" / "secondary").is_dir()
    state = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert [item["name"] for item in state["profiles"]] == ["main", "secondary"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_detached_tmux_session_keeps_auto_switching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, fake_home, project = _setup(tmp_path)
    ccs_wrapper, fake_bin = _ccs_source_wrapper(tmp_path)
    socket = "ccs-test-" + uuid.uuid4().hex[:12]
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    env["CC_SWAPER_TMUX_SOCKET"] = socket
    name = session_name(project)
    try:
        launched = subprocess.run(
            [str(ccs_wrapper), "run"], env=env, cwd=project,
            capture_output=True, text=True, timeout=6,
        )
        assert launched.returncode == 0, launched.stderr
        assert "Started background session" in launched.stdout
        sessions_file = tmp_path / "store" / "sessions.json"
        deadline = time.monotonic() + 12
        record = None
        while time.monotonic() < deadline:
            if sessions_file.exists():
                values = json.loads(sessions_file.read_text())["sessions"]
                record = next(iter(values.values()), None)
                if isinstance(record, dict) and record.get("profile") == "secondary":
                    break
            time.sleep(0.1)
        assert isinstance(record, dict) and record["profile"] == "secondary"
        assert Path(record["path"]).is_file()

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("PATH", env["PATH"])
        monkeypatch.setenv("CC_SWAPER_TMUX_SOCKET", socket)
        snapshot = _snapshot(ProfileStore(tmp_path / "store"))
        assert snapshot["sessions"][0]["name"] == name
        assert snapshot["sessions"][0]["profile"] == "secondary"
    finally:
        subprocess.run(
            [shutil.which("tmux") or "tmux", "-L", socket, "kill-session", "-t", f"={name}"],
            env=env, capture_output=True, text=True, check=False,
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_background_attach_detach_and_stop_cleans_child(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    ccs_wrapper, fake_bin = _ccs_source_wrapper(tmp_path)
    socket = "ccs-test-" + uuid.uuid4().hex[:12]
    env.update({
        "PATH": str(fake_bin) + os.pathsep + env["PATH"],
        "CC_SWAPER_TMUX_SOCKET": socket,
        "FAKE_MODE": "wait_no_quota",
        "FAKE_PID_FILE": str(tmp_path / "claude.pid"),
        "TERM": "xterm-256color",
    })
    env.pop("TMUX", None)
    name = session_name(project)
    tmux = shutil.which("tmux") or "tmux"
    try:
        launched = subprocess.run(
            [str(ccs_wrapper), "run"], env=env, cwd=project,
            capture_output=True, text=True, timeout=6,
        )
        assert launched.returncode == 0, launched.stderr
        deadline = time.monotonic() + 8
        pid_file = Path(env["FAKE_PID_FILE"])
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.1)
        assert pid_file.is_file()
        child_pid = int(pid_file.read_text())

        attach_pid, master = pty.fork()
        if attach_pid == 0:
            os.chdir(project)
            os.execve(str(ccs_wrapper), [str(ccs_wrapper), "attach"], env)
        attach_status = None
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                state = subprocess.run(
                    [tmux, "-L", socket, "list-sessions", "-F", "#{session_attached}"],
                    env=env, capture_output=True, text=True,
                )
                if state.returncode == 0 and state.stdout.strip() == "1":
                    break
                time.sleep(0.1)
            os.write(master, b"\x02d")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                waited, status = os.waitpid(attach_pid, os.WNOHANG)
                if waited:
                    attach_status = status
                    break
                time.sleep(0.1)
            assert attach_status is not None
            assert os.waitstatus_to_exitcode(attach_status) == 0
        finally:
            if attach_status is None:
                os.kill(attach_pid, signal.SIGTERM)
                os.waitpid(attach_pid, 0)
            os.close(master)
        still_running = subprocess.run(
            [tmux, "-L", socket, "has-session", "-t", f"={name}"],
            env=env, capture_output=True, text=True,
        )
        assert still_running.returncode == 0

        stopped = subprocess.run(
            [str(ccs_wrapper), "stop"], env=env, cwd=project,
            capture_output=True, text=True, timeout=6,
        )
        assert stopped.returncode == 0, stopped.stderr
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Claude child survived ccs stop")
    finally:
        subprocess.run(
            [tmux, "-L", socket, "kill-session", "-t", f"={name}"],
            env=env, capture_output=True, text=True, check=False,
        )
