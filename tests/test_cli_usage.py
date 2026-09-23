from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

from cc_swaper import cli
from cc_swaper.profiles import ProfileStore
from cc_swaper.usage import UsageError, UsageSnapshot


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProfileStore:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    store.add_managed("second")
    monkeypatch.setattr(cli, "claude_binary", lambda: "/trusted/claude")
    return store


def test_usage_lists_all_profiles_with_bars_and_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store(tmp_path, monkeypatch)
    calls: list[str] = []

    def fake_fetch(profile, _binary, timeout=45, cancel_event=None):
        calls.append(profile.name)
        return UsageSnapshot(
            profile=profile, plan="max",
            session_percent=23 if profile.name == "main" else 100,
            session_reset_text="Sep 23 at 4:09pm (Asia/Saigon)",
            weekly_percent=18, weekly_reset_text="Sep 25 at 1:59am (Asia/Saigon)",
            model_weekly=[("Fable", 0, None)],
        )

    monkeypatch.setattr("cc_swaper.usage.fetch_usage", fake_fetch)
    assert cli.main(["usage"]) == 0
    output = capsys.readouterr().out
    assert set(calls) == {"main", "second"}
    assert "| Account" in output and "| Mốc" in output
    assert "main (max)" in output and "second (max)" in output
    assert "[██░░░░░░░░] 23%" in output
    assert "[██████████] 100%" in output
    assert "[██░░░░░░░░] 18%" in output
    assert "7 ngày (Fable)" in output
    assert "[░░░░░░░░░░] 0%" in output
    assert "Sep 25 at 1:59am" in output
    assert "chưa có mốc reset" in output
    assert "subscription ends at" not in output


def test_usage_json_uses_null_for_unavailable_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "cc_swaper.usage.fetch_usage",
        lambda profile, _binary, timeout=45, cancel_event=None: UsageSnapshot(
            profile=profile, plan="pro", session_percent=0,
            session_reset_text=None, weekly_percent=0,
            weekly_reset_text="Sep 27 at 8pm (Asia/Saigon)",
        ),
    )
    assert cli.main(["usage", "--json", "main"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["accounts"]) == 1
    account = payload["accounts"][0]
    assert account["profile"] == "main"
    assert account["five_hour"] == {"used_percent": 0, "resets_at": None}
    assert "subscription_ends_at" not in account


def test_usage_shows_loading_inside_interactive_table_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(tmp_path, monkeypatch)

    class InteractiveStdout(io.StringIO):
        def isatty(self) -> bool:
            return True

    screen = InteractiveStdout()
    monkeypatch.setattr(cli.sys, "stdout", screen)
    monkeypatch.setenv("TERM", "xterm-256color")

    def fake_fetch(profile, _binary, timeout=45, cancel_event=None):
        time.sleep(0.2)
        return UsageSnapshot(
            profile=profile, plan="max", session_percent=25,
            session_reset_text="Sep 23 at 4pm", weekly_percent=10,
            weekly_reset_text="Sep 25 at 2am",
        )

    monkeypatch.setattr("cc_swaper.usage.fetch_usage", fake_fetch)
    assert cli.main(["usage", "main"]) == 0
    output = screen.getvalue()
    assert "\x1b[?1049h" in output and "\x1b[?1049l" in output
    assert "Đang tải" in output and "main" in output
    final_table = output.split("\x1b[?1049l", 1)[1]
    assert "[███░░░░░░░] 25%" in final_table
    assert "Đang tải" not in final_table

    screen.seek(0)
    screen.truncate()
    assert cli.main(["usage", "--json", "main"]) == 0
    assert "\x1b" not in screen.getvalue()
    assert json.loads(screen.getvalue())["accounts"][0]["profile"] == "main"


def test_usage_fetches_profiles_concurrently_but_preserves_requested_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store(tmp_path, monkeypatch)
    together = threading.Barrier(2)

    def fake_fetch(profile, _binary, timeout=45, cancel_event=None):
        together.wait(timeout=2)
        if profile.name == "second":
            time.sleep(0.05)
        return UsageSnapshot(
            profile=profile, plan="max", session_percent=20,
            session_reset_text="tomorrow", weekly_percent=40,
            weekly_reset_text="next week",
        )

    monkeypatch.setattr("cc_swaper.usage.fetch_usage", fake_fetch)
    assert cli.main(["usage", "--json", "second", "main"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [account["profile"] for account in payload["accounts"]] == ["second", "main"]


def test_live_table_updates_one_account_while_another_is_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(tmp_path, monkeypatch)
    first_row_updated = threading.Event()

    class InteractiveStdout(io.StringIO):
        def isatty(self) -> bool:
            return True

        def write(self, value: str) -> int:
            if "second (max)" in value and "✓ Xong" in value and "Đang tải" in value:
                first_row_updated.set()
            return super().write(value)

    screen = InteractiveStdout()
    monkeypatch.setattr(cli.sys, "stdout", screen)
    monkeypatch.setenv("TERM", "xterm-256color")

    def fake_fetch(profile, _binary, timeout=45, cancel_event=None):
        if profile.name == "main":
            assert first_row_updated.wait(timeout=3)
        return UsageSnapshot(
            profile=profile, plan="max", session_percent=20,
            session_reset_text="tomorrow", weekly_percent=40,
            weekly_reset_text="next week",
        )

    monkeypatch.setattr("cc_swaper.usage.fetch_usage", fake_fetch)
    assert cli.main(["usage"]) == 0
    assert first_row_updated.is_set()
    final_table = screen.getvalue().split("\x1b[?1049l", 1)[1]
    assert "main (max)" in final_table and "second (max)" in final_table
    assert "Đang tải" not in final_table


def test_usage_ctrl_c_cancels_running_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(tmp_path, monkeypatch)
    started = threading.Event()
    stopped = threading.Event()

    class InteractiveStdout(io.StringIO):
        def isatty(self) -> bool:
            return True

    screen = InteractiveStdout()
    monkeypatch.setattr(cli.sys, "stdout", screen)
    monkeypatch.setenv("TERM", "xterm-256color")

    def fake_fetch(profile, _binary, timeout=45, cancel_event=None):
        started.set()
        assert cancel_event is not None
        assert cancel_event.wait(timeout=2)
        stopped.set()
        raise UsageError("usage check cancelled")

    def interrupted_wait(*_args, **_kwargs):
        assert started.wait(timeout=2)
        raise KeyboardInterrupt

    monkeypatch.setattr("cc_swaper.usage.fetch_usage", fake_fetch)
    monkeypatch.setattr(cli, "wait", interrupted_wait)
    assert cli.main(["usage", "main"]) == 130
    assert stopped.is_set()
    assert "\x1b[?1049h" in screen.getvalue()
    assert "\x1b[?1049l" in screen.getvalue()


def test_usage_reports_one_profile_failure_without_losing_other_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store(tmp_path, monkeypatch)

    def fake_fetch(profile, _binary, timeout=45, cancel_event=None):
        if profile.name == "second":
            raise UsageError("Claude usage command timed out")
        return UsageSnapshot(
            profile=profile, plan="max", session_percent=10,
            session_reset_text="Sep 23 at 4pm", weekly_percent=5,
            weekly_reset_text="Sep 25 at 2am",
        )

    monkeypatch.setattr("cc_swaper.usage.fetch_usage", fake_fetch)
    assert cli.main(["usage", "--json"]) == 1
    accounts = json.loads(capsys.readouterr().out)["accounts"]
    assert accounts[0]["five_hour"]["used_percent"] == 10
    assert accounts[1] == {
        "profile": "second", "error": "Claude usage command timed out"
    }
    assert cli.main(["usage"]) == 1
    table = capsys.readouterr().out
    assert "main (max)" in table
    assert "Lỗi" in table and "Claude usage command timed out" in table
