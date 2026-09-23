from __future__ import annotations

import io
import json
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

    def fake_fetch(profile, _binary, timeout=45):
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
    assert calls == ["main", "second"]
    assert "5 giờ: [█████░░░░░░░░░░░░░░░] 23% đã dùng" in output
    assert "5 giờ: [████████████████████] 100% đã dùng" in output
    assert "7 ngày: [████░░░░░░░░░░░░░░░░] 18% đã dùng" in output
    assert "7 ngày (Fable): [░░░░░░░░░░░░░░░░░░░░] 0% đã dùng" in output
    assert "reset: Sep 25 at 1:59am (Asia/Saigon)" in output
    assert "reset: Claude chưa cung cấp mốc reset" in output
    assert "subscription ends at" not in output


def test_usage_json_uses_null_for_unavailable_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "cc_swaper.usage.fetch_usage",
        lambda profile, _binary, timeout=45: UsageSnapshot(
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


def test_usage_shows_loading_only_in_interactive_text_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store(tmp_path, monkeypatch)

    class InteractiveStderr(io.StringIO):
        def isatty(self) -> bool:
            return True

    progress = InteractiveStderr()
    monkeypatch.setattr(cli.sys, "stderr", progress)

    def fake_fetch(profile, _binary, timeout=45):
        time.sleep(0.15)
        return UsageSnapshot(
            profile=profile, plan="max", session_percent=25,
            session_reset_text="Sep 23 at 4pm", weekly_percent=10,
            weekly_reset_text="Sep 25 at 2am",
        )

    monkeypatch.setattr("cc_swaper.usage.fetch_usage", fake_fetch)
    assert cli.main(["usage", "main"]) == 0
    assert "Đang kiểm tra usage 1/1: main" in progress.getvalue()
    assert "✓ main (" in progress.getvalue()

    progress.seek(0)
    progress.truncate()
    assert cli.main(["usage", "--json", "main"]) == 0
    assert progress.getvalue() == ""


def test_usage_reports_one_profile_failure_without_losing_other_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store(tmp_path, monkeypatch)

    def fake_fetch(profile, _binary, timeout=45):
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
