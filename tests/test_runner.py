from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from cc_swaper.cli import _claude_args, _parser
from cc_swaper.hooks import HookMonitor, hook_settings, minimal_event, write_event
from cc_swaper.profiles import Profile
from cc_swaper.runner import profile_environment, run_passthrough


SESSION_ID = "550e8400-e29b-41d4-a716-446655440000"


def test_profile_environment_omits_default_config_and_credential_overrides(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/wrong")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "secret")
    monkeypatch.setenv("ANTHROPIC_PROFILE", "wrong")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "Authorization: secret")
    monkeypatch.setenv("NODE_OPTIONS", "--require ./malicious.js")
    monkeypatch.setenv("CC_SWAPER_RUN_ID", "a1b2c3d4e5f6")

    default_env = profile_environment(Profile("main", None))
    assert "CLAUDE_CONFIG_DIR" not in default_env
    assert not any(key in default_env for key in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
        "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS", "NODE_OPTIONS",
        "CC_SWAPER_RUN_ID",
    ))
    config_dir = tmp_path / "second"
    config_dir.mkdir()
    config_dir.chmod(0o700)
    managed_env = profile_environment(Profile("second", config_dir))
    assert managed_env["CLAUDE_CONFIG_DIR"] == str(config_dir)


def test_hooks_report_only_identity_for_a_real_usage_limit(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    transcript = project_dir / f"{SESSION_ID}.jsonl"
    transcript.touch()
    base = {
        "session_id": SESSION_ID,
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
    }
    start = minimal_event({**base, "hook_event_name": "SessionStart", "source": "startup"})
    assert start == {"type": "start", "session_id": SESSION_ID, "transcript_path": str(transcript)}
    assert minimal_event({
        **base, "hook_event_name": "StopFailure", "error": "rate_limit",
        "error_details": "429 Too Many Requests",
        "last_assistant_message": "API Error: Rate limit reached",
    }) is None
    quota = minimal_event({
        **base, "hook_event_name": "StopFailure", "error": "rate_limit",
        "last_assistant_message": "You've hit your limit · resets 8am",
    })
    assert quota == {"type": "limit", "session_id": SESSION_ID, "transcript_path": str(transcript)}
    assert minimal_event({
        **base, "hook_event_name": "StopFailure", "error": "authentication_failed",
        "last_assistant_message": "You've hit your limit · resets 8am",
    }) is None
    assert minimal_event({
        **base, "hook_event_name": "StopFailure", "error": "rate_limit",
        "error_details": "This request would exceed your account's rate limit. Please try again later.",
    }) == quota
    for banner in (
        "You've hit your weekly limit · resets 8am",
        "You've hit your 5-hour limit · resets 8am",
        "Usage limit reached ∙ resets 8am",
        "5-hour limit reached ∙ resets 8am",
    ):
        assert minimal_event({
            **base, "hook_event_name": "StopFailure", "error": "rate_limit",
            "last_assistant_message": banner,
        }) == quota
    assert minimal_event({
        **base, "hook_event_name": "Stop",
        "last_assistant_message": "5-hour limit reached ∙ resets 8am",
    }) is None
    assert minimal_event({**base, "hook_event_name": "SessionStart", "agent_id": "child"}) is None

    event_file = tmp_path / "events.jsonl"
    event_file.touch(mode=0o600)
    write_event(event_file, start)
    write_event(event_file, quota)
    with HookMonitor(
        event_file, profile_projects=tmp_path, expected_session_id=SESSION_ID
    ) as monitor:
        monitor.poll()
        assert monitor.session_id == SESSION_ID
        assert monitor.transcript_path == transcript
        assert monitor.quota
    settings = json.loads(hook_settings(event_file))
    assert set(settings["hooks"]) == {"SessionStart", "StopFailure"}
    assert settings["hooks"]["StopFailure"][0]["matcher"] == "rate_limit"


def test_hook_monitor_rejects_mismatched_transcript_name(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    wrong = project / "wrong-name.jsonl"
    wrong.touch()
    event_file = tmp_path / "events.jsonl"
    event_file.touch(mode=0o600)
    for kind in ("start", "limit"):
        write_event(event_file, {
            "type": kind, "session_id": SESSION_ID, "transcript_path": str(wrong)
        })
    with HookMonitor(
        event_file, profile_projects=tmp_path, expected_session_id=SESSION_ID
    ) as monitor:
        monitor.poll()
        assert monitor.session_id is None
        assert not monitor.quota


def test_resume_parser_accepts_profile_after_session_id() -> None:
    parsed = _parser().parse_args([
        "resume", SESSION_ID, "--profile", "second", "--no-auto"
    ])
    assert parsed.session == SESSION_ID
    assert parsed.profile == "second"
    assert parsed.no_auto


@pytest.mark.parametrize("flag", ["--bare", "--safe-mode", "--no-session-persistence"])
def test_auto_rejects_modes_without_hooks_or_transcripts(flag: str) -> None:
    with pytest.raises(ValueError):
        _claude_args([flag], auto=True)


def test_monitored_passthrough_notifies_before_session_exits(tmp_path: Path) -> None:
    finished = tmp_path / "finished"

    class Monitor:
        session_id: str | None = None
        polls = 0

        def poll(self) -> None:
            self.polls += 1
            if self.polls >= 2:
                self.session_id = SESSION_ID

    monitor = Monitor()
    seen_before_exit: list[bool] = []
    code = run_passthrough(
        sys.executable,
        ["-c", "import sys,time; time.sleep(0.35); open(sys.argv[1], 'w').write('done')", str(finished)],
        dict(os.environ), monitor=monitor,
        on_session_started=lambda: seen_before_exit.append(not finished.exists()),
    )
    assert code == 0
    assert seen_before_exit == [True]
    assert finished.read_text() == "done"
