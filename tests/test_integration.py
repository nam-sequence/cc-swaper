from __future__ import annotations

import json
import hashlib
import fcntl
import os
import pty
import re
import select
import signal
import shutil
import subprocess
import sys
import termios
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import pytest

from cc_swaper.profiles import ProfileStore
from cc_swaper.service import _snapshot
from cc_swaper.tmux_sessions import session_name


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys, time, uuid
from pathlib import Path

args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    if os.environ.get("FAKE_AUTH_WAIT_FILE"):
        Path(os.environ["FAKE_AUTH_WAITING_MARKER"]).touch()
        deadline = time.monotonic() + 10
        while not Path(os.environ["FAKE_AUTH_WAIT_FILE"]).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    if os.environ.get("FAKE_AUTH_STATUS_FAIL"):
        sys.exit(1)
    if os.environ.get("CLAUDE_CONFIG_DIR") and (Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".fake_logged_out").exists():
        print(json.dumps({"loggedIn": False, "authMethod": "none"}))
        sys.exit(1)
    profile = "secondary" if os.environ.get("CLAUDE_CONFIG_DIR") else "main"
    if os.environ.get("FAKE_DISTINCT_MANAGED") and os.environ.get("CLAUDE_CONFIG_DIR"):
        profile = Path(os.environ["CLAUDE_CONFIG_DIR"]).name
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
    if os.environ.get("FAKE_MODE") == "exit_127":
        print("EXEC FAILED", flush=True)
        sys.exit(127)
    if os.environ.get("FAKE_DELAY_START_FILE"):
        Path(os.environ["FAKE_WAITING_MARKER"]).touch()
        deadline = time.monotonic() + 10
        while not Path(os.environ["FAKE_DELAY_START_FILE"]).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
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
    if os.environ.get("FAKE_CHAIN_QUOTA") and os.environ.get("CLAUDE_CONFIG_DIR"):
        profile_name = Path(os.environ["CLAUDE_CONFIG_DIR"]).name
        if profile_name == "secondary":
            event_file.open("a").write(json.dumps({"type": "limit", "session_id": sid, "transcript_path": str(path)}) + "\n")
            print("SECONDARY_LIMIT", flush=True)
            time.sleep(30)
        if profile_name == "third":
            print("THIRD_READY", flush=True)
            time.sleep(30)
    print("RESUMED:" + sid, flush=True)
    if os.environ.get("FAKE_MODE") == "wait_after_resume":
        time.sleep(30)
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


def _started_session_name(output: str, project: Path) -> str:
    match = re.search(r"Started background session (ccs-[a-z0-9-]+)\.", output)
    assert match is not None, output
    name = match.group(1)
    assert name.startswith(session_name(project) + "-")
    return name


def _run_tty(
    env: dict[str, str], project: Path, command: tuple[str, ...] = ("run", "--foreground"),
    *, terminate_on: str | None = None, choice: str | None = None,
    detach_on: str | None = None,
    on_marker: tuple[str, Callable[[], None]] | None = None,
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
    choice_sent = False
    detach_sent = False
    marker_handled = False
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
                if choice and not choice_sent and "Enter a number or account name:" in output.decode(errors="replace"):
                    os.write(master, choice.encode())
                    choice_sent = True
                if detach_on and not detach_sent and detach_on in output.decode(errors="replace"):
                    os.write(master, b"\x02d")
                    detach_sent = True
                if on_marker and not marker_handled and on_marker[0] in output.decode(errors="replace"):
                    on_marker[1]()
                    marker_handled = True
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


def test_manual_switch_menu_forks_last_session(tmp_path: Path) -> None:
    env, home, project = _setup(tmp_path)
    env["FAKE_MODE"] = "normal"
    initial_code, initial_output, _ = _run_tty(env, project)
    assert initial_code == 0, initial_output

    switch_code, switch_output, _ = _run_tty(
        env, project, ("switch", "--foreground"), choice="2\n"
    )
    assert switch_code == 0, switch_output
    assert "2. secondary" in switch_output
    assert "RESUMED:" in switch_output
    main_file = next((home / ".claude" / "projects" / "test-project").glob("*.jsonl"))
    second_file = next((tmp_path / "store" / "profiles" / "secondary" / "projects" / "test-project").glob("*.jsonl"))
    assert main_file.read_text() == "original transcript\n"
    assert second_file.read_text() == "original transcript\nforked\n"


def test_failed_manual_switch_does_not_change_selected_profile(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "cc_swaper", "switch", "--foreground", "secondary"],
        env=env, cwd=project, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert "session ID" in result.stderr
    profiles = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert profiles["selected"] == "main"


def test_switch_rolls_back_selection_when_target_profile_is_locked(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    env["FAKE_MODE"] = "normal"
    initial_code, initial_output, _ = _run_tty(env, project)
    assert initial_code == 0, initial_output

    lock_dir = tmp_path / "store" / "locks"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    lock = lock_dir / "profile-secondary.lock"
    with lock.open("w+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = subprocess.run(
            [sys.executable, "-m", "cc_swaper", "switch", "--foreground", "secondary"],
            env=env, cwd=project, capture_output=True, text=True, timeout=5,
        )
        fcntl.flock(handle, fcntl.LOCK_UN)
    assert result.returncode == 2
    assert "currently in use" in result.stderr
    profiles = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert profiles["selected"] == "main"


def test_switch_exec_failure_restores_selection_with_existing_target_transcript(
    tmp_path: Path,
) -> None:
    env, _, project = _setup(tmp_path)
    env["FAKE_MODE"] = "normal"
    initial_code, initial_output, _ = _run_tty(env, project)
    assert initial_code == 0, initial_output
    switch_code, switch_output, _ = _run_tty(
        env, project, ("switch", "--foreground", "secondary")
    )
    assert switch_code == 0, switch_output
    use_result = subprocess.run(
        [sys.executable, "-m", "cc_swaper", "use", "main"],
        env=env, cwd=project, capture_output=True, text=True, timeout=5,
    )
    assert use_result.returncode == 0, use_result.stderr

    env["FAKE_MODE"] = "exit_127"
    failed_code, failed_output, _ = _run_tty(
        env, project, ("switch", "--foreground", "secondary")
    )
    assert failed_code == 127, failed_output
    profiles = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert profiles["selected"] == "main"


def test_switch_start_does_not_override_a_newer_account_choice(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    add = subprocess.run(
        [sys.executable, "-m", "cc_swaper", "add", "third", "--no-login"],
        env=env, cwd=project, capture_output=True, text=True, timeout=5,
    )
    assert add.returncode == 0, add.stderr
    env["FAKE_MODE"] = "normal"
    initial_code, initial_output, _ = _run_tty(env, project)
    assert initial_code == 0, initial_output

    release = tmp_path / "release-start"
    waiting = tmp_path / "waiting-start"
    env["FAKE_DELAY_START_FILE"] = str(release)
    env["FAKE_WAITING_MARKER"] = str(waiting)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _run_tty, env, project, ("switch", "--foreground", "secondary")
        )
        try:
            deadline = time.monotonic() + 5
            while not waiting.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert waiting.exists()
            changed = subprocess.run(
                [sys.executable, "-m", "cc_swaper", "use", "third"],
                env=env, cwd=project, capture_output=True, text=True, timeout=5,
            )
            assert changed.returncode == 0, changed.stderr
        finally:
            release.touch()
        code, output, _ = future.result(timeout=15)
    assert code == 0, output
    assert "RESUMED:" in output
    profiles = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert profiles["selected"] == "third"


def test_switch_preflight_does_not_override_a_newer_account_choice(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    add = subprocess.run(
        [sys.executable, "-m", "cc_swaper", "add", "third", "--no-login"],
        env=env, cwd=project, capture_output=True, text=True, timeout=5,
    )
    assert add.returncode == 0, add.stderr
    env["FAKE_MODE"] = "normal"
    initial_code, initial_output, _ = _run_tty(env, project)
    assert initial_code == 0, initial_output

    release = tmp_path / "release-auth"
    waiting = tmp_path / "waiting-auth"
    env["FAKE_AUTH_WAIT_FILE"] = str(release)
    env["FAKE_AUTH_WAITING_MARKER"] = str(waiting)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _run_tty, env, project, ("switch", "--foreground", "secondary")
        )
        try:
            deadline = time.monotonic() + 5
            while not waiting.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert waiting.exists()
            changed = subprocess.run(
                [sys.executable, "-m", "cc_swaper", "use", "third"],
                env=env, cwd=project, capture_output=True, text=True, timeout=5,
            )
            assert changed.returncode == 0, changed.stderr
        finally:
            release.touch()
        code, output, _ = future.result(timeout=15)
    assert code == 0, output
    assert "RESUMED:" in output
    profiles = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert profiles["selected"] == "third"


def test_adding_account_does_not_cancel_in_progress_switch(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    env["FAKE_MODE"] = "normal"
    initial_code, initial_output, _ = _run_tty(env, project)
    assert initial_code == 0, initial_output

    release = tmp_path / "release-start"
    waiting = tmp_path / "waiting-start"
    env["FAKE_DELAY_START_FILE"] = str(release)
    env["FAKE_WAITING_MARKER"] = str(waiting)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _run_tty, env, project, ("switch", "--foreground", "secondary")
        )
        try:
            deadline = time.monotonic() + 5
            while not waiting.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert waiting.exists()
            added = subprocess.run(
                [sys.executable, "-m", "cc_swaper", "add", "third", "--no-login"],
                env=env, cwd=project, capture_output=True, text=True, timeout=5,
            )
            assert added.returncode == 0, added.stderr
        finally:
            release.touch()
        code, output, _ = future.result(timeout=15)
    assert code == 0, output
    profiles = json.loads((tmp_path / "store" / "profiles.json").read_text())
    assert profiles["selected"] == "secondary"


def test_auto_switch_waits_for_failed_turn_to_flush(tmp_path: Path) -> None:
    env, home, project = _setup(tmp_path)
    env["FAKE_MODE"] = "late_flush"
    code, output, _ = _run_tty(env, project)
    assert code == 0, output
    main_file = next((home / ".claude" / "projects" / "test-project").glob("*.jsonl"))
    second_file = next((tmp_path / "store" / "profiles" / "secondary" / "projects" / "test-project").glob("*.jsonl"))
    assert "late persisted" in main_file.read_text()
    assert "late persisted" in second_file.read_text()


def test_later_quota_handoff_keeps_previous_transcript_profile_locked(tmp_path: Path) -> None:
    env, _, project = _setup(tmp_path)
    added = subprocess.run(
        [sys.executable, "-m", "cc_swaper", "add", "third", "--no-login"],
        env=env, cwd=project, capture_output=True, text=True, timeout=5,
    )
    assert added.returncode == 0, added.stderr
    env["FAKE_CHAIN_QUOTA"] = "1"
    env["FAKE_DISTINCT_MANAGED"] = "1"
    removal: list[subprocess.CompletedProcess[str]] = []
    duplicate_resume: list[subprocess.CompletedProcess[str]] = []

    def try_remove_secondary() -> None:
        removal.append(subprocess.run(
            [sys.executable, "-m", "cc_swaper", "remove", "secondary"],
            env=env, cwd=project, capture_output=True, text=True, timeout=5,
        ))
        third_dir = tmp_path / "store" / "profiles" / "third" / "projects" / "test-project"
        third_id = next(third_dir.glob("*.jsonl")).stem
        digest = hashlib.sha256(third_id.encode()).hexdigest()[:24]
        lock_path = tmp_path / "store" / "locks" / f"conversation-{digest}.lock"
        deadline = time.monotonic() + 3
        locked = False
        while time.monotonic() < deadline:
            if lock_path.exists():
                with lock_path.open("r+") as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        locked = True
                    else:
                        fcntl.flock(handle, fcntl.LOCK_UN)
            if locked:
                break
            time.sleep(0.02)
        assert locked
        duplicate_resume.append(subprocess.run(
            [sys.executable, "-m", "cc_swaper", "resume", "--foreground", third_id],
            env=env, cwd=project, capture_output=True, text=True, timeout=5,
        ))

    code, output, _ = _run_tty(
        env, project, terminate_on="THIRD_READY",
        on_marker=("THIRD_READY", try_remove_secondary),
    )
    assert code == 128 + signal.SIGTERM, output
    assert "SECONDARY_LIMIT" in output and "THIRD_READY" in output
    assert len(removal) == 1
    assert removal[0].returncode != 0
    assert "currently in use" in removal[0].stderr
    assert len(duplicate_resume) == 1
    assert duplicate_resume[0].returncode != 0
    assert "already running" in duplicate_resume[0].stderr
    assert (tmp_path / "store" / "profiles" / "secondary").is_dir()


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
    try:
        launched = subprocess.run(
            [str(ccs_wrapper), "run"], env=env, cwd=project,
            capture_output=True, text=True, timeout=6,
        )
        assert launched.returncode == 0, launched.stderr
        assert "Started background session" in launched.stdout
        name = _started_session_name(launched.stdout, project)
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
            [shutil.which("tmux") or "tmux", "-L", socket, "kill-server"],
            env=env, capture_output=True, text=True, check=False,
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_manual_menu_starts_detached_session_on_selected_profile(tmp_path: Path) -> None:
    env, home, project = _setup(tmp_path)
    env["FAKE_MODE"] = "normal"
    initial_code, initial_output, _ = _run_tty(env, project)
    assert initial_code == 0, initial_output

    _ccs_wrapper, fake_bin = _ccs_source_wrapper(tmp_path)
    socket = "ccs-test-" + uuid.uuid4().hex[:12]
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    env["CC_SWAPER_TMUX_SOCKET"] = socket
    env["FAKE_MODE"] = "wait_after_resume"
    try:
        code, output, _ = _run_tty(env, project, ("switch",), choice="2\n")
        assert code == 0, output
        assert "Started background session" in output
        _started_session_name(output, project)

        target_dir = tmp_path / "store" / "profiles" / "secondary" / "projects" / "test-project"
        deadline = time.monotonic() + 12
        target_files: list[Path] = []
        selected = None
        while time.monotonic() < deadline:
            selected = json.loads((tmp_path / "store" / "profiles.json").read_text())["selected"]
            target_files = list(target_dir.glob("*.jsonl"))
            if selected == "secondary" and target_files:
                break
            time.sleep(0.1)
        assert selected == "secondary"
        assert len(target_files) == 1
        assert target_files[0].read_text() == "original transcript\nforked\n"
        main_file = next((home / ".claude" / "projects" / "test-project").glob("*.jsonl"))
        assert main_file.read_text() == "original transcript\n"
    finally:
        subprocess.run(
            [shutil.which("tmux") or "tmux", "-L", socket, "kill-server"],
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
    tmux = shutil.which("tmux") or "tmux"
    try:
        launched = subprocess.run(
            [str(ccs_wrapper), "run"], env=env, cwd=project,
            capture_output=True, text=True, timeout=6,
        )
        assert launched.returncode == 0, launched.stderr
        name = _started_session_name(launched.stdout, project)
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
            [tmux, "-L", socket, "kill-server"],
            env=env, capture_output=True, text=True, check=False,
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_two_background_runs_in_one_project_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, fake_home, project = _setup(tmp_path)
    ccs_wrapper, fake_bin = _ccs_source_wrapper(tmp_path)
    socket = "ccs-test-" + uuid.uuid4().hex[:12]
    env.update({
        "PATH": str(fake_bin) + os.pathsep + env["PATH"],
        "CC_SWAPER_TMUX_SOCKET": socket,
        "FAKE_MODE": "wait_no_quota",
    })
    tmux = shutil.which("tmux") or "tmux"
    try:
        launched = [
            subprocess.run(
                [str(ccs_wrapper), "run"], env=env, cwd=project,
                capture_output=True, text=True, timeout=6,
            )
            for _ in range(2)
        ]
        assert all(result.returncode == 0 for result in launched), launched
        names = [_started_session_name(result.stdout, project) for result in launched]
        assert names[0] != names[1]
        run_ids = [name.rsplit("-", 1)[1] for name in names]

        sidecar = tmp_path / "store" / "background-sessions.json"
        deadline = time.monotonic() + 8
        records = {}
        while time.monotonic() < deadline:
            if sidecar.exists():
                records = json.loads(sidecar.read_text()).get("runs", {})
            if all(run_id in records and records[run_id].get("path") for run_id in run_ids):
                break
            time.sleep(0.1)
        assert set(run_ids).issubset(records)
        assert records[run_ids[0]]["id"] != records[run_ids[1]]["id"]

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("PATH", env["PATH"])
        monkeypatch.setenv("CC_SWAPER_TMUX_SOCKET", socket)
        snapshot = _snapshot(ProfileStore(tmp_path / "store"))
        rows = [row for row in snapshot["sessions"] if row["cwd"] == str(project)]
        assert {row["run_id"] for row in rows} == set(run_ids)
        assert {row["session_id"] for row in rows} == {
            records[run_ids[0]]["id"], records[run_ids[1]]["id"],
        }

        stopped = subprocess.run(
            [str(ccs_wrapper), "stop", "--session", run_ids[0]],
            env=env, cwd=project, capture_output=True, text=True, timeout=6,
        )
        assert stopped.returncode == 0, stopped.stderr
        first = subprocess.run(
            [tmux, "-L", socket, "has-session", "-t", f"={names[0]}"],
            env=env, capture_output=True, text=True,
        )
        second = subprocess.run(
            [tmux, "-L", socket, "has-session", "-t", f"={names[1]}"],
            env=env, capture_output=True, text=True,
        )
        assert first.returncode != 0 and second.returncode == 0
        remaining = json.loads(sidecar.read_text())["runs"]
        assert run_ids[0] not in remaining and run_ids[1] in remaining
    finally:
        subprocess.run(
            [tmux, "-L", socket, "kill-server"],
            env=env, capture_output=True, text=True, check=False,
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_interactive_run_attaches_new_session_then_detaches_without_stopping(
    tmp_path: Path,
) -> None:
    env, _, project = _setup(tmp_path)
    _ccs_wrapper, fake_bin = _ccs_source_wrapper(tmp_path)
    socket = "ccs-test-" + uuid.uuid4().hex[:12]
    env.update({
        "PATH": str(fake_bin) + os.pathsep + env["PATH"],
        "CC_SWAPER_TMUX_SOCKET": socket,
        "FAKE_MODE": "wait_no_quota",
        "TERM": "xterm-256color",
    })
    env.pop("TMUX", None)
    tmux = shutil.which("tmux") or "tmux"
    try:
        code, output, _ = _run_tty(env, project, ("run",), detach_on="WAITING")
        assert code == 0, output
        assert "Detached from background session" in output
        match = re.search(r"Detached from background session (ccs-[a-z0-9-]+);", output)
        assert match is not None, output
        name = match.group(1)
        assert name.startswith(session_name(project) + "-")
        still_running = subprocess.run(
            [tmux, "-L", socket, "has-session", "-t", f"={name}"],
            env=env, capture_output=True, text=True,
        )
        assert still_running.returncode == 0
    finally:
        subprocess.run(
            [tmux, "-L", socket, "kill-server"],
            env=env, capture_output=True, text=True, check=False,
        )
