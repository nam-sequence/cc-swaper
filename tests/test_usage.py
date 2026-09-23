from __future__ import annotations

import json
import subprocess

import pytest

from cc_swaper.profiles import Profile
from cc_swaper.usage import (
    UsageError,
    UsageSnapshot,
    fetch_usage,
    parse_usage_output,
    parse_usage_text,
)


PROFILE = Profile("main", None)


def _response(result: str, **extra: object) -> str:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "local_command": "usage",
        "num_turns": 0,
        "total_cost_usd": 0,
        "result": result,
    }
    payload.update(extra)
    return json.dumps(payload)


def test_parse_usage_output_preserves_reset_text_and_model_weekly() -> None:
    result = "\n".join(
        [
            "You are currently using your subscription to power your Claude Code usage",
            "",
            "Current session: 23% used · resets Sep 23 at 4:09pm (Asia/Saigon)",
            "Current week (all models): 18% used · resets Sep 25 at 1:59am (Asia/Saigon)",
            "Current week (Fable): 0% used · resets Sep 25 at 2am (Asia/Saigon)",
        ]
    )

    snapshot = parse_usage_output(_response(result), PROFILE, plan="max")

    assert isinstance(snapshot, UsageSnapshot)
    assert snapshot.profile == PROFILE
    assert snapshot.plan == "max"
    assert snapshot.session_percent == 23
    assert snapshot.session_reset_text == "Sep 23 at 4:09pm (Asia/Saigon)"
    assert snapshot.weekly_percent == 18
    assert snapshot.weekly_reset_text == "Sep 25 at 1:59am (Asia/Saigon)"
    assert snapshot.model_weekly == [("Fable", 0, "Sep 25 at 2am (Asia/Saigon)")]
    assert snapshot.subscription_end is None


def test_parse_usage_text_allows_reset_to_be_absent_at_zero_percent() -> None:
    snapshot = parse_usage_text(
        "Current session: 0% used\nCurrent week (all models): 0% used",
        PROFILE,
    )

    assert snapshot.session_percent == 0
    assert snapshot.session_reset_text is None
    assert snapshot.weekly_percent == 0
    assert snapshot.weekly_reset_text is None


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        json.dumps({"local_command": "usage", "result": 42}),
        _response("No quota lines here", local_command="other"),
        _response("Current session: 101% used"),
    ],
)
def test_parse_usage_output_rejects_malformed_or_nonlocal_responses(output: str) -> None:
    with pytest.raises(UsageError):
        parse_usage_output(output, PROFILE)


@pytest.mark.parametrize(
    "field,value",
    [("num_turns", 1), ("total_cost_usd", 0.01)],
)
def test_parse_usage_output_rejects_model_requests(field: str, value: object) -> None:
    with pytest.raises(UsageError, match="non-local"):
        parse_usage_output(
            _response("Current session: 12% used", **{field: value}), PROFILE
        )


def test_fetch_usage_uses_supported_non_persistent_local_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        "cc_swaper.usage.auth_details",
        lambda profile, binary: {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
        },
    )
    monkeypatch.setattr(
        "cc_swaper.usage.profile_environment",
        lambda profile: {"CLAUDE_CONFIG_DIR": "/private/profile"},
    )

    def fake_run(args, **kwargs):
        calls.append((args, kwargs["env"]))
        return subprocess.CompletedProcess(args, 0, _response("Current session: 12% used"), "")

    monkeypatch.setattr("cc_swaper.usage.subprocess.run", fake_run)

    snapshot = fetch_usage(PROFILE, "/opt/claude", timeout=3)

    assert snapshot.plan == "max"
    assert snapshot.session_percent == 12
    assert calls == [
        (
            [
                "/opt/claude",
                "-p",
                "--no-session-persistence",
                "--output-format",
                "json",
                "/usage",
            ],
            {"CLAUDE_CONFIG_DIR": "/private/profile"},
        )
    ]


def test_fetch_usage_hides_failed_process_output(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "private workspace details and an auth token"
    monkeypatch.setattr(
        "cc_swaper.usage.auth_details",
        lambda profile, binary: {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
        },
    )
    monkeypatch.setattr(
        "cc_swaper.usage.profile_environment",
        lambda profile: {},
    )
    monkeypatch.setattr(
        "cc_swaper.usage.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, secret, secret
        ),
    )

    with pytest.raises(UsageError) as caught:
        fetch_usage(PROFILE, "/opt/claude")
    assert secret not in str(caught.value)


def test_fetch_usage_handles_timeout_without_leaking_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cc_swaper.usage.auth_details",
        lambda profile, binary: {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
        },
    )
    monkeypatch.setattr(
        "cc_swaper.usage.profile_environment",
        lambda profile: {},
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output="secret")

    monkeypatch.setattr("cc_swaper.usage.subprocess.run", timeout)

    with pytest.raises(UsageError, match="timed out"):
        fetch_usage(PROFILE, "/opt/claude", timeout=0.1)


@pytest.mark.parametrize(
    "status",
    [
        {"loggedIn": True, "authMethod": "apiKey"},
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"},
    ],
)
def test_fetch_usage_rejects_unsupported_auth(
    monkeypatch: pytest.MonkeyPatch, status: dict[str, object]
) -> None:
    monkeypatch.setattr("cc_swaper.usage.auth_details", lambda profile, binary: status)
    with pytest.raises(UsageError, match="supported claude.ai"):
        fetch_usage(PROFILE, "/opt/claude")
