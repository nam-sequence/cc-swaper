from __future__ import annotations

import json
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


def test_usage_lists_all_profiles_and_marks_missing_subscription_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store(tmp_path, monkeypatch)
    calls: list[str] = []

    def fake_fetch(profile, _binary, timeout=45):
        calls.append(profile.name)
        return UsageSnapshot(
            profile=profile, plan="max", session_percent=23,
            session_reset_text="Sep 23 at 4:09pm (Asia/Saigon)",
            weekly_percent=18, weekly_reset_text="Sep 25 at 1:59am (Asia/Saigon)",
            model_weekly=[("Fable", 0, None)], subscription_end=None,
        )

    monkeypatch.setattr("cc_swaper.usage.fetch_usage", fake_fetch)
    assert cli.main(["usage"]) == 0
    output = capsys.readouterr().out
    assert calls == ["main", "second"]
    assert "5 giờ: 23% đã dùng" in output
    assert "7 ngày: 18% đã dùng" in output
    assert "Sep 25 at 1:59am (Asia/Saigon)" in output
    assert "subscription ends at: không có dữ liệu" in output


def test_usage_json_uses_null_for_unavailable_reset_and_subscription_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path, monkeypatch)
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
    assert account["subscription_ends_at"] is None


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
