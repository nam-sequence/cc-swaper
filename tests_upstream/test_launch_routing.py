"""Race checks between account activation and profile launch routing."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from cc_swaper.profiles import LaunchRouteChanged, ProfileStore, SelectionSnapshot
from claude_swap import cli
from claude_swap.models import Platform
from claude_swap.cli import _read_route_snapshot_from_env
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui.app import _route_manual_switch
from claude_swap.tui.data import run_action
from claude_swap.autoswitch import AutoSwitchEngine, NoSwitchEvent, TickOutcome
from claude_swap.settings import AutoSwitchSettings


def _profile_store(temp_home: Path) -> ProfileStore:
    return ProfileStore(home=temp_home / ".config" / "cc-swaper")


def _switcher(temp_home: Path) -> ClaudeAccountSwitcher:
    switcher = ClaudeAccountSwitcher()
    switcher.platform = Platform.MACOS
    switcher._setup_directories()
    switcher._write_json(switcher.sequence_file, {
        "activeAccountNumber": None,
        "lastUpdated": "2026-01-01T00:00:00Z",
        "sequence": [1],
        "accounts": {
            "1": {
                "email": "target@example.com",
                "uuid": "target-uuid",
                "organizationUuid": "",
                "organizationName": "",
                "added": "2026-01-01T00:00:00Z",
            }
        },
    })
    return switcher


def _perform_inputs(switcher: ClaudeAccountSwitcher, write_credentials):
    target_credentials = json.dumps({
        "claudeAiOauth": {
            "accessToken": "target-access",
            "refreshToken": "target-refresh",
            "expiresAt": 4_000_000_000_000,
        }
    })
    target_config = json.dumps({
        "oauthAccount": {"emailAddress": "target@example.com", "accountUuid": "target-uuid"}
    })
    return (
        patch.object(switcher, "_read_target_credentials", return_value=target_credentials),
        patch.object(switcher, "_read_account_config", return_value=target_config),
        patch.object(switcher, "_read_credentials", return_value=""),
        patch.object(switcher, "_prepare_credentials_for_activation", return_value=target_credentials),
        patch.object(switcher, "_write_credentials", side_effect=write_credentials),
    )


def test_auto_switch_gate_refuses_a_legacy_launch_backend(temp_home: Path):
    store = _profile_store(temp_home)
    store.add_default("main")
    snapshot = store.selection_snapshot()
    switcher = _switcher(temp_home)

    patches = _perform_inputs(switcher, lambda *_: None)
    with patches[0], patches[1], patches[2], patches[3], patches[4] as write_active:
        with pytest.raises(LaunchRouteChanged):
            switcher._perform_switch(
                "1", emit_output=False, force_activate=True,
                activation_snapshot=snapshot,
                automatic_activation=True,
            )

    write_active.assert_not_called()


def test_profile_selection_waits_until_auto_activation_write_finishes(
    temp_home: Path,
):
    store = _profile_store(temp_home)
    store.add_default("main")
    store.use_accounts()
    snapshot = store.selection_snapshot()
    switcher = _switcher(temp_home)
    started = threading.Event()
    selected = threading.Event()
    write_observed_block = []

    def select_legacy_profile():
        started.set()
        store.select("main")
        selected.set()

    selector = threading.Thread(target=select_legacy_profile)

    def write_active(_credentials):
        selector.start()
        assert started.wait(2)
        write_observed_block.append(not selected.wait(0.05))

    patches = _perform_inputs(switcher, write_active)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        switcher._perform_switch(
            "1", emit_output=False, force_activate=True,
            activation_snapshot=snapshot,
            automatic_activation=True,
        )
    selector.join(timeout=2)

    assert not selector.is_alive()
    assert write_observed_block == [True]
    assert store.launch_backend() == "legacy"


def test_auto_tick_pauses_when_legacy_profile_backend_is_selected(temp_home: Path):
    store = _profile_store(temp_home)
    store.add_default("main")
    events = []

    class Switcher:
        def set_poll_policy_inputs(self, *_args):
            pass

        def current_account_number(self):
            raise AssertionError("tick should pause before reading account state")

    engine = AutoSwitchEngine(
        Switcher(), AutoSwitchSettings(), events.append,
        state_path=temp_home / "autoswitch.json",
    )
    assert engine.tick() is TickOutcome.NO_ACTION
    assert isinstance(events[-1], NoSwitchEvent)
    assert events[-1].reason == "legacy-backend"


def test_auto_switch_rejects_aba_route_change_at_activation(temp_home: Path):
    store = _profile_store(temp_home)
    store.add_default("main")
    store.use_accounts()
    snapshot = store.selection_snapshot()
    store.select("main")
    store.use_accounts()  # returns to accounts, but revision now differs
    events = []

    class Switcher:
        def set_poll_policy_inputs(self, *_args):
            pass

        def switch_to(self, _number, **kwargs):
            with store.accounts_activation_guard(kwargs["activation_snapshot"]):
                raise AssertionError("stale snapshots must not activate")

    engine = AutoSwitchEngine(
        Switcher(), AutoSwitchSettings(), events.append,
        state_path=temp_home / "autoswitch.json",
    )
    result = engine._perform(
        "1", "target@example.com", "failover", (None, 0.0), snapshot
    )

    assert result is TickOutcome.NO_ACTION
    assert isinstance(events[-1], NoSwitchEvent)
    assert events[-1].reason == "route-changed"
    assert not (temp_home / "autoswitch.json").exists()


def test_auto_switch_reports_legacy_if_profile_wins_before_activation(temp_home: Path):
    store = _profile_store(temp_home)
    store.add_default("main")
    store.use_accounts()
    snapshot = store.selection_snapshot()
    store.select("main")
    events = []

    class Switcher:
        def set_poll_policy_inputs(self, *_args):
            pass

        def switch_to(self, _number, **kwargs):
            with store.accounts_activation_guard(kwargs["activation_snapshot"]):
                raise AssertionError("legacy route must not activate")

    engine = AutoSwitchEngine(
        Switcher(), AutoSwitchSettings(), events.append,
        state_path=temp_home / "autoswitch.json",
    )
    result = engine._perform(
        "1", "target@example.com", "failover", (None, 0.0), snapshot
    )

    assert result is TickOutcome.NO_ACTION
    assert isinstance(events[-1], NoSwitchEvent)
    assert events[-1].reason == "legacy-backend"
    assert not (temp_home / "autoswitch.json").exists()


def test_tui_manual_switch_routes_accounts_if_selection_is_unchanged(temp_home: Path):
    store = _profile_store(temp_home)
    store.add_default("main")
    payload = _route_manual_switch(lambda _snapshot: {"switched": True}, profile_store=store)
    assert store.launch_backend() == "accounts"
    assert payload["launchBackend"] == "accounts"
    assert payload["routingChanged"] is True


def test_tui_manual_switch_does_not_overwrite_newer_legacy_selection(temp_home: Path):
    store = _profile_store(temp_home)
    store.add_default("main")

    def switch_while_a_new_profile_choice_arrives(_snapshot):
        store.select("main")
        return {"switched": True}

    payload = _route_manual_switch(
        switch_while_a_new_profile_choice_arrives,
        profile_store=store,
    )
    assert store.launch_backend() == "legacy"
    assert payload["routingChanged"] is False
    assert payload["routingWarning"]


def test_manual_switch_gate_rejects_a_stale_route_before_active_write(
    temp_home: Path,
):
    store = _profile_store(temp_home)
    store.add_default("main")
    snapshot = store.selection_snapshot()
    store.use_accounts()  # a newer route choice invalidates the CLI snapshot
    switcher = _switcher(temp_home)

    patches = _perform_inputs(switcher, lambda *_: None)
    with patches[0], patches[1], patches[2], patches[3], patches[4] as write_active:
        with pytest.raises(LaunchRouteChanged):
            switcher._perform_switch(
                "1", emit_output=False, force_activate=True,
                activation_snapshot=snapshot,
            )

    write_active.assert_not_called()


def test_manual_switch_guard_serializes_profile_selection_with_active_write(
    temp_home: Path,
):
    store = _profile_store(temp_home)
    store.add_default("main")
    snapshot = store.selection_snapshot()
    switcher = _switcher(temp_home)
    started = threading.Event()
    selected = threading.Event()
    write_observed_block = []

    def select_legacy_profile():
        started.set()
        store.select("main")
        selected.set()

    selector = threading.Thread(target=select_legacy_profile)

    def write_active(_credentials):
        selector.start()
        assert started.wait(2)
        write_observed_block.append(not selected.wait(0.05))

    patches = _perform_inputs(switcher, write_active)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        switcher._perform_switch(
            "1", emit_output=False, force_activate=True,
            activation_snapshot=snapshot,
        )
    selector.join(timeout=2)

    assert not selector.is_alive()
    assert write_observed_block == [True]
    assert store.launch_backend() == "legacy"


def test_manual_switch_rollback_completes_before_waiting_route_selection(
    temp_home: Path,
):
    store = _profile_store(temp_home)
    store.add_default("main")
    snapshot = store.selection_snapshot()
    switcher = _switcher(temp_home)
    started = threading.Event()
    selected = threading.Event()
    writes: list[str] = []
    write_observed_block = []

    def select_legacy_profile():
        started.set()
        store.select("main")
        selected.set()

    selector = threading.Thread(target=select_legacy_profile)

    def write_active(credentials):
        writes.append(credentials)
        if len(writes) == 1:
            selector.start()
            assert started.wait(2)
            write_observed_block.append(not selected.wait(0.05))

    patches = _perform_inputs(switcher, write_active)
    config_path = switcher._get_claude_config_path()
    write_json = switcher._write_json

    def fail_config_commit(path, data):
        if Path(path) == config_path:
            raise OSError("injected config commit failure")
        return write_json(path, data)

    with patches[0], patches[1], patches[3], patches[4], \
         patch.object(switcher, "_read_credentials", return_value="old-live-credentials"), \
         patch.object(switcher, "_get_current_account", return_value=("old@example.com", "")), \
         patch.object(switcher, "_stash_live_credential", return_value="saved"), \
         patch.object(switcher, "_write_json", side_effect=fail_config_commit):
        with pytest.raises(OSError, match="injected config commit failure"):
            switcher._perform_switch(
                "1",
                emit_output=False,
                force_activate=True,
                activation_snapshot=snapshot,
            )

    selector.join(timeout=2)
    assert not selector.is_alive()
    assert write_observed_block == [True]
    assert writes == [
        json.dumps({
            "claudeAiOauth": {
                "accessToken": "target-access",
                "refreshToken": "target-refresh",
                "expiresAt": 4_000_000_000_000,
            }
        }),
        "old-live-credentials",
    ]
    assert writes[-1] == "old-live-credentials"
    assert store.launch_backend() == "legacy"


def test_route_snapshot_env_is_validated_and_consumed(monkeypatch):
    monkeypatch.setenv(
        "CC_SWAPER_ROUTE_SNAPSHOT",
        json.dumps({"name": "main", "revision": 8, "launch_backend": "legacy"}),
    )
    assert _read_route_snapshot_from_env() == SelectionSnapshot("main", 8, "legacy")
    assert "CC_SWAPER_ROUTE_SNAPSHOT" not in os.environ


def test_invalid_route_snapshot_env_fails_closed(monkeypatch):
    monkeypatch.setenv(
        "CC_SWAPER_ROUTE_SNAPSHOT",
        json.dumps({"name": "main", "revision": True, "launch_backend": "legacy"}),
    )
    with pytest.raises(ValueError, match="invalid account-routing snapshot"):
        _read_route_snapshot_from_env()


@pytest.mark.parametrize(
    ("argv", "method", "args", "kwargs"),
    [
        (
            ["--switch", "--json"],
            "switch",
            (),
            {"strategy": None, "json_output": True, "models": (), "model_source": None},
        ),
        (
            ["--switch-to=2", "--json"],
            "switch_to",
            ("2",),
            {"json_output": True, "force": False},
        ),
    ],
)
def test_cli_switch_routes_snapshot_and_reports_route_race_as_json(
    monkeypatch, capsys, argv, method, args, kwargs,
):
    snapshot = {"name": "main", "revision": 12, "launch_backend": "legacy"}
    monkeypatch.setenv("CC_SWAPER_ROUTE_SNAPSHOT", json.dumps(snapshot))
    with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
         patch("claude_swap.cli.sys.argv", ["ccs", *argv]), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch("claude_swap.update_check.check_for_update", return_value=None):
        operation = getattr(switcher_cls.return_value, method)
        operation.side_effect = LaunchRouteChanged(
            "account switch stopped because the Claude launch route changed"
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()

    assert exc.value.code == 1
    operation.assert_called_once_with(
        *args,
        **kwargs,
        activation_snapshot=SelectionSnapshot("main", 12, "legacy"),
    )
    assert json.loads(capsys.readouterr().out) == {
        "schemaVersion": 1,
        "error": {
            "type": "LaunchRouteChanged",
            "message": "account switch stopped because the Claude launch route changed",
        },
    }


def test_cli_switch_route_race_is_a_clean_human_error(monkeypatch, capsys):
    monkeypatch.setenv(
        "CC_SWAPER_ROUTE_SNAPSHOT",
        json.dumps({"name": "main", "revision": 12, "launch_backend": "legacy"}),
    )
    with patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls, \
         patch("claude_swap.cli.sys.argv", ["ccs", "--switch"]), \
         patch("os.geteuid", return_value=1000, create=True), \
         patch("claude_swap.update_check.check_for_update", return_value=None):
        switcher_cls.return_value.switch.side_effect = LaunchRouteChanged(
            "account switch stopped because the Claude launch route changed"
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()

    assert exc.value.code == 1
    output = capsys.readouterr()
    assert "account switch stopped because the Claude launch route changed" in (
        output.out + output.err
    )
    assert "Traceback" not in output.out + output.err


def test_tui_stale_manual_route_returns_failed_action_without_traceback(
    temp_home: Path,
):
    store = _profile_store(temp_home)
    store.add_default("main")
    switcher = _switcher(temp_home)

    def change_route_before_activation(snapshot):
        store.select("main")
        return switcher._perform_switch(
            "1",
            emit_output=False,
            force_activate=True,
            activation_snapshot=snapshot,
        )

    patches = _perform_inputs(switcher, lambda *_: None)
    with patches[0], patches[1], patches[2], patches[3], patches[4] as write_active:
        result = run_action(
            lambda: _route_manual_switch(change_route_before_activation, store)
        )

    write_active.assert_not_called()
    assert result.ok is False
    assert "Error: account switch stopped because the Claude launch route changed" in result.output
