"""The legacy profile selector and account engine share one Claude launcher."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cc_swaper import cli
from cc_swaper.profiles import LaunchRouteChanged, ProfileStore


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProfileStore:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CC_SWAPER_HOME", str(tmp_path / "store"))
    store = ProfileStore()
    store.add_default("main")
    store.add_managed("work")
    return store


def test_old_registry_migrates_to_legacy_and_new_registry_has_explicit_backend(
    tmp_path: Path,
) -> None:
    store = ProfileStore(tmp_path / "store")
    assert store.launch_backend() == "accounts"
    store.add_default("main")
    state_path = store.home / "profiles.json"
    state = json.loads(state_path.read_text())
    assert state["version"] == 2
    assert state["launch_backend"] == "legacy"

    state["version"] = 1
    state.pop("launch_backend")
    state_path.write_text(json.dumps(state))
    reopened = ProfileStore(store.home)
    assert reopened.launch_backend() == "legacy"
    reopened.use_accounts()
    upgraded = json.loads(state_path.read_text())
    assert upgraded["version"] == 2
    assert upgraded["launch_backend"] == "accounts"
    reopened.select("main")
    assert ProfileStore(store.home).launch_backend() == "legacy"


def test_accounts_switch_routes_new_claude_sessions_without_copying_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/wrong/inherited/profile")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/wrong/storage")
    monkeypatch.setenv("SSLKEYLOGFILE", "/fake/tls-keys.log")
    payload = {
        "schemaVersion": 1,
        "switched": True,
        "from": {"number": None, "email": "other@example.test"},
        "to": {"number": 2, "email": "next@example.test"},
    }
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        route = json.loads(kwargs["env"]["CC_SWAPER_ROUTE_SNAPSHOT"])
        assert route["name"] == "main"
        assert route["launch_backend"] == "legacy"
        assert isinstance(route["revision"], int)
        assert "CLAUDE_CONFIG_DIR" not in kwargs["env"]
        assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in kwargs["env"]
        assert "SSLKEYLOGFILE" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["accounts", "switch", "2", "--json"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert calls == [[sys.executable, "-I", "-m", "claude_swap", "switch", "2", "--json"]]
    assert response["launchBackend"] == "accounts"
    assert response["routingChanged"] is True
    assert ProfileStore(store.home).selected().name == "main"
    assert ProfileStore(store.home).launch_backend() == "accounts"

    monkeypatch.setattr(cli, "claude_binary", lambda: "/fake/claude")
    launches: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        cli, "run_passthrough",
        lambda _binary, args, env: launches.append((args, env)) or 0,
    )
    assert cli.main(["native", "--", "--version"]) == 0
    assert launches[0][0] == ["--version"]
    assert "CLAUDE_CONFIG_DIR" not in launches[0][1]
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in launches[0][1]

    monkeypatch.setattr(cli, "auth_status", lambda *_args: (True, "claude.ai"))
    assert cli.main(["switch", "work", "--json"]) == 0
    assert ProfileStore(store.home).launch_backend() == "legacy"
    assert cli.main(["native", "--", "--version"]) == 0
    assert launches[-1][1]["CLAUDE_CONFIG_DIR"] == str(store.profiles_dir / "work")


def test_engine_switch_does_not_override_a_newer_legacy_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path, monkeypatch)

    def fake_run(command, **_kwargs):
        ProfileStore(store.home).select("work")
        return subprocess.CompletedProcess(
            command, 0,
            json.dumps({"schemaVersion": 1, "switched": True, "to": {"number": 2}}),
            "",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["accounts", "switch", "2", "--json"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["routingChanged"] is False
    assert response["launchBackend"] == "legacy"
    assert ProfileStore(store.home).selected().name == "work"


@pytest.mark.parametrize("args", [
    ["--switch-to=2", "--json"],
    ["--json", "--switch-to=2"],
    ["--switch-to", "2", "--json"],
    ["--switch", "--json"],
])
def test_engine_legacy_flag_spellings_also_update_launch_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
) -> None:
    store = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0,
            json.dumps({"schemaVersion": 1, "switched": True, "to": {"number": 2}}),
            "",
        ),
    )
    assert cli.main(["accounts", *args]) == 0
    assert json.loads(capsys.readouterr().out)["launchBackend"] == "accounts"
    assert ProfileStore(store.home).launch_backend() == "accounts"


def test_auto_pauses_for_legacy_route_and_resumes_after_accounts_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path, monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "call", lambda command, **_kwargs: calls.append(command) or 2)
    assert cli.main(["accounts", "auto", "--once", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "legacy-backend"
    assert calls == []

    assert cli.main(["accounts", "use", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["launchBackend"] == "accounts"
    assert ProfileStore(store.home).launch_backend() == "accounts"
    assert cli.main(["accounts", "auto", "--once", "--json"]) == 2
    assert calls == [[sys.executable, "-I", "-m", "claude_swap", "auto", "--once", "--json"]]


def test_routing_json_is_read_only_and_does_not_query_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _store(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "claude_binary", lambda: pytest.fail("queried Claude"))
    assert cli.main(["routing", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schemaVersion": 1,
        "launchBackend": "legacy",
        "selectedLegacyProfile": "main",
    }


def test_auto_activation_guard_rejects_legacy_and_route_aba(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "store")
    store.add_default("main")
    store.use_accounts()
    original = store.selection_snapshot()
    with store.accounts_activation_guard(original):
        pass

    store.select("main")
    with pytest.raises(LaunchRouteChanged, match="route changed"):
        with store.accounts_activation_guard(original):
            pytest.fail("entered unsafe activation")

    store.use_accounts()
    assert store.launch_backend() == "accounts"
    with pytest.raises(LaunchRouteChanged, match="route changed"):
        with store.accounts_activation_guard(original):
            pytest.fail("accepted stale ABA snapshot")


def test_manual_activation_guard_rejects_newer_profile_selection(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "store")
    store.add_default("main")
    store.add_managed("work")
    original = store.selection_snapshot()
    with store.manual_accounts_activation_guard(original):
        pass
    store.select("work")
    with pytest.raises(LaunchRouteChanged, match="route changed"):
        with store.manual_accounts_activation_guard(original):
            pytest.fail("entered stale manual activation")


def test_switch_reports_partial_success_if_routing_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0,
            json.dumps({"schemaVersion": 1, "switched": True, "to": {"number": 2}}),
            "",
        ),
    )
    monkeypatch.setattr(
        ProfileStore,
        "use_accounts_if_unchanged",
        lambda *_args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    assert cli.main(["accounts", "switch", "2", "--json"]) == 1
    response = json.loads(capsys.readouterr().out)
    assert response["error"]["code"] == "routing_update_failed"
    assert "ccs accounts use" in response["error"]["message"]
    assert response["switchResult"]["to"]["number"] == 2
    assert ProfileStore(store.home).launch_backend() == "legacy"


def test_human_switch_reports_partial_success_if_routing_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.subprocess, "call", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(
        ProfileStore,
        "use_accounts_if_unchanged",
        lambda *_args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    assert cli.main(["accounts", "switch", "2"]) == 1
    assert "account credentials switched" in capsys.readouterr().err
    assert ProfileStore(store.home).launch_backend() == "legacy"


def test_human_switch_success_routes_new_claude_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path, monkeypatch)
    commands: list[list[str]] = []

    def fake_call(command, **kwargs):
        commands.append(command)
        assert json.loads(kwargs["env"]["CC_SWAPER_ROUTE_SNAPSHOT"])["launch_backend"] == "legacy"
        return 0

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    assert cli.main(["accounts", "switch", "2"]) == 0
    assert commands == [[sys.executable, "-I", "-m", "claude_swap", "switch", "2"]]
    assert "New Claude sessions will use this account" in capsys.readouterr().out
    assert ProfileStore(store.home).launch_backend() == "accounts"


def test_success_exit_with_invalid_engine_json_warns_about_unknown_switch_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "truncated {", ""),
    )

    assert cli.main(["accounts", "switch", "2", "--json"]) == 1
    response = json.loads(capsys.readouterr().out)
    assert response["error"]["code"] == "switch_response_invalid"
    assert "may have changed" in response["error"]["message"]
    assert "ccs accounts status" in response["error"]["message"]
    assert ProfileStore(store.home).launch_backend() == "legacy"
