"""Purge admission and lifetime-lease regression tests."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.admission import EngineAdmissionGate
from claude_swap.exceptions import CredentialError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord, UsageEntry


_EMAIL = "gate@example.com"
_OLD = json.dumps({
    "claudeAiOauth": {
        "accessToken": "old-access",
        "refreshToken": "old-refresh",
        "expiresAt": 1_000,
    }
})
_NEW = json.dumps({
    "claudeAiOauth": {
        "accessToken": "new-access",
        "refreshToken": "new-refresh",
        "expiresAt": 9_999_999_999_000,
    }
})


def _switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.LINUX
    switcher._setup_directories()
    switcher._write_json(switcher.sequence_file, {
        "activeAccountNumber": None,
        "lastUpdated": "2026-09-29T00:00:00Z",
        "sequence": [1],
        "accounts": {
            "1": {
                "email": _EMAIL,
                "uuid": "gate-account",
                "organizationUuid": "",
                "organizationName": "",
                "added": "2026-09-29T00:00:00Z",
            },
        },
    })
    switcher._write_account_credentials("1", _EMAIL, _OLD)
    return switcher


def test_purge_drains_inflight_refresh_and_rejects_late_consumers(
    temp_home: Path, monkeypatch
):
    switcher = _switcher(temp_home)
    post_entered = threading.Event()
    release_post = threading.Event()
    purge_closed_admission = threading.Event()
    failures: dict[str, list[BaseException]] = {"consumer": [], "purge": []}
    outcomes = []

    def fake_refresh(credentials: str, **_kwargs):
        assert credentials == _OLD
        post_entered.set()
        if not release_post.wait(10):
            raise AssertionError("test did not release refresh POST")
        return oauth.RefreshOutcome(_NEW, None)

    real_write_state = EngineAdmissionGate._write_state

    def observe_closing(self, state: str):
        result = real_write_state(self, state)
        if state == "closing":
            purge_closed_admission.set()
        return result

    monkeypatch.setattr(oauth, "try_refresh_oauth_credentials", fake_refresh)
    monkeypatch.setattr(EngineAdmissionGate, "_write_state", observe_closing)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    def consume_worker():
        try:
            outcomes.append(switcher.consume_backup_grant("1", _EMAIL, _OLD))
        except BaseException as exc:
            failures["consumer"].append(exc)

    def purge_worker():
        try:
            switcher.purge()
        except BaseException as exc:
            failures["purge"].append(exc)

    consumer = threading.Thread(target=consume_worker, name="inflight-consumer")
    purge = threading.Thread(target=purge_worker, name="purge-waiter")
    consumer.start()
    try:
        assert post_entered.wait(5), "consumer did not enter its network POST"
        purge.start()
        assert purge_closed_admission.wait(5), "purge did not close admission"

        # The active consume still owns the shared lifetime lease, so purge
        # cannot delete its store until the CAS/stash tail completes.
        assert switcher.backup_dir.exists()

        late = switcher.consume_backup_grant("1", _EMAIL, _OLD)
        assert late.error == "engine-closed"
        assert switcher.backup_dir.exists()
    finally:
        release_post.set()
        consumer.join(10)
        if purge.ident is not None:
            purge.join(10)

    assert not consumer.is_alive()
    assert not purge.is_alive()
    assert failures == {"consumer": [], "purge": []}
    assert len(outcomes) == 1 and outcomes[0].error is None
    assert not switcher.backup_dir.exists()
    state = json.loads(
        (switcher.backup_dir.parent / ".account-engine.state.json").read_text()
    )
    assert state["state"] == "purged"

    # A delayed collector callback after purge has completed must not make
    # FileLock.acquire recreate credentials/ or restore its spent successor.
    after = switcher.consume_backup_grant("1", _EMAIL, _OLD)
    assert after.error == "engine-closed"
    assert not switcher.backup_dir.exists()


def test_explicit_account_add_reopens_a_purged_engine(temp_home: Path, monkeypatch):
    switcher = _switcher(temp_home)
    with patch("builtins.input", return_value="y"):
        switcher.purge()
    assert not switcher.backup_dir.exists()

    switcher.add_account_from_token(
        "oauth-setup-token", email="reopened@example.com", slot=1, assume_yes=True
    )

    sequence = switcher._get_sequence_data()
    assert sequence["accounts"]["1"]["email"] == "reopened@example.com"
    state = json.loads(
        (switcher.backup_dir.parent / ".account-engine.state.json").read_text()
    )
    assert state["state"] == "active"


def test_passive_update_cache_cannot_recreate_purged_engine(temp_home: Path, monkeypatch):
    from claude_swap.admission import AdmissionClosed
    from claude_swap.cache import write_cache

    switcher = _switcher(temp_home)
    with patch("builtins.input", return_value="y"):
        switcher.purge()
    assert not switcher.backup_dir.exists()

    with pytest.raises(AdmissionClosed):
        write_cache(switcher.backup_dir / "cache" / "update_check.json", "0.9.1")
    assert not switcher.backup_dir.exists()


def test_lazy_logging_does_not_recreate_a_purged_engine(temp_home: Path, monkeypatch):
    switcher = _switcher(temp_home)
    with patch("builtins.input", return_value="y"):
        switcher.purge()
    assert not switcher.backup_dir.exists()

    fresh_switcher = ClaudeAccountSwitcher()
    fresh_switcher._logger.info("post-purge diagnostic")
    assert not switcher.backup_dir.exists()


def test_usage_fetch_holds_lifetime_lease_through_cache_write(
    temp_home: Path, monkeypatch
):
    switcher = _switcher(temp_home)
    usage_started = threading.Event()
    release_usage = threading.Event()
    purge_closed_admission = threading.Event()
    failures: dict[str, list[BaseException]] = {"usage": [], "purge": []}
    result: dict[str, dict[str, UsageEntry]] = {}
    info = [(1, _EMAIL, "", "", False, _OLD, "")]

    def fake_fetch(_info, _rejected_fp=None):
        usage_started.set()
        if not release_usage.wait(10):
            raise AssertionError("test did not release usage GET")
        return FetchRecord(usage={"five_hour": {"pct": 10}})

    real_write_state = EngineAdmissionGate._write_state

    def observe_closing(self, state: str):
        result = real_write_state(self, state)
        if state == "closing":
            purge_closed_admission.set()
        return result

    monkeypatch.setattr(switcher, "_fetch_account_usage", fake_fetch)
    monkeypatch.setattr(EngineAdmissionGate, "_write_state", observe_closing)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    def usage_worker():
        try:
            result.update(switcher._collect_usage_entries(info))
        except BaseException as exc:
            failures["usage"].append(exc)

    def purge_worker():
        try:
            switcher.purge()
        except BaseException as exc:
            failures["purge"].append(exc)

    usage = threading.Thread(target=usage_worker, name="usage-fetch")
    purge = threading.Thread(target=purge_worker, name="purge-waits-for-usage")
    usage.start()
    try:
        assert usage_started.wait(5), "usage fetch did not enter the simulated GET"
        purge.start()
        assert purge_closed_admission.wait(5), "purge did not close admission"
        late = switcher._collect_usage_entries(info)
        assert late["1"].fetched_at is None
        assert switcher.backup_dir.exists()
    finally:
        release_usage.set()
        usage.join(10)
        if purge.ident is not None:
            purge.join(10)

    assert not usage.is_alive()
    assert not purge.is_alive()
    assert failures == {"usage": [], "purge": []}
    assert result["1"].fetched_at is not None
    assert not switcher.backup_dir.exists()

    # A late collector after purge returns an empty view without creating
    # cache/usage.json or any other account-engine path.
    after = switcher._collect_usage_entries(info)
    assert after["1"].fetched_at is None
    assert not switcher.backup_dir.exists()


def test_partial_purge_stays_closed_until_retry_succeeds(
    temp_home: Path, monkeypatch
):
    switcher = _switcher(temp_home)
    switcher.platform = Platform.MACOS
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    with patch(
        "claude_swap.switcher.macos_keychain.delete_password",
        side_effect=RuntimeError("injected keychain denial"),
    ):
        with pytest.raises(CredentialError, match="retry purge"):
            switcher.purge()

    state_path = switcher.backup_dir.parent / ".account-engine.state.json"
    assert json.loads(state_path.read_text())["state"] == "closing"
    late = switcher.consume_backup_grant("1", _EMAIL, _OLD)
    assert late.error == "engine-closed"
    assert switcher.backup_dir.exists()

    with patch("claude_swap.switcher.macos_keychain.delete_password"):
        switcher.purge()

    assert not switcher.backup_dir.exists()
    assert json.loads(state_path.read_text())["state"] == "purged"
