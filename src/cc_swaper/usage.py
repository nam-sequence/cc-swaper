"""Read Claude Code usage information through its supported local command.

The usage command is intentionally run in print mode without session
persistence.  This module does not read Claude's credential files or call an
undocumented service API; authentication and profile isolation stay inside the
helpers in :mod:`cc_swaper.runner`.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

from .process import ProcessCancelled, run_cancelable
from .profiles import Profile
from .runner import auth_details, profile_environment


_MAX_OUTPUT_CHARS = 256_000
_USAGE_LINE = re.compile(
    r"^Current\s+(?P<kind>session|week)"
    r"(?:\s*\((?P<label>[^)\r\n]+)\))?\s*:\s*"
    r"(?P<percent>\d{1,3})%\s+used"
    r"(?:\s*(?:[·•∙⋅]|-)\s*resets\s+(?P<reset>.*?))?\s*$",
    re.IGNORECASE,
)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class UsageError(RuntimeError):
    """Raised when Claude cannot provide a valid usage snapshot."""


@dataclass(frozen=True)
class UsageSnapshot:
    """A point-in-time usage report for one configured profile.

    Reset values intentionally retain Claude's source wording, including the
    timezone in parentheses when Claude provides one.
    """

    profile: Profile
    plan: str | None
    session_percent: int | None
    session_reset_text: str | None
    weekly_percent: int | None
    weekly_reset_text: str | None
    model_weekly: list[tuple[str, int, str | None]] = field(default_factory=list)


def _plan_from_auth(status: Mapping[str, Any]) -> str | None:
    """Extract a displayable plan without depending on one status version."""

    for key in ("plan", "subscriptionType", "subscription_type", "tier"):
        value = status.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _reset_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _percent(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 100:
        raise UsageError("Claude returned an invalid usage percentage")
    return parsed


def parse_usage_text(
    text: str,
    profile: Profile,
    *,
    plan: str | None = None,
) -> UsageSnapshot:
    """Parse the human-readable ``result`` from Claude's usage command.

    Unknown lines are ignored so Claude can add explanatory text without
    breaking the parser.  At least one recognized current session or week
    line is required; an output with no usage data is treated as malformed.
    """

    if not isinstance(text, str):
        raise UsageError("Claude returned a non-text usage result")

    session_percent: int | None = None
    session_reset: str | None = None
    weekly_percent: int | None = None
    weekly_reset: str | None = None
    model_weekly: list[tuple[str, int, str | None]] = []

    # Claude currently emits plain text, but strip terminal control sequences
    # before matching in case a future version colors the usage panel.
    for raw_line in text.splitlines():
        line = _ANSI_ESCAPE.sub("", raw_line).strip()
        match = _USAGE_LINE.fullmatch(line)
        if match is None:
            continue

        kind = match.group("kind").casefold()
        label = match.group("label")
        percentage = _percent(match.group("percent"))
        reset = _reset_text(match.group("reset"))

        if kind == "session":
            if session_percent is not None:
                raise UsageError("Claude returned duplicate session usage data")
            session_percent = percentage
            session_reset = reset
            continue

        # ``Current week (all models)`` is the aggregate weekly quota.  Other
        # labels identify model-specific weekly quotas.
        if label is None or label.strip().casefold() == "all models":
            if weekly_percent is not None:
                raise UsageError("Claude returned duplicate weekly usage data")
            weekly_percent = percentage
            weekly_reset = reset
        else:
            model_weekly.append((label.strip(), percentage, reset))

    if session_percent is None and weekly_percent is None and not model_weekly:
        raise UsageError("Claude returned no recognized usage data")

    return UsageSnapshot(
        profile=profile,
        plan=plan.strip() if isinstance(plan, str) and plan.strip() else None,
        session_percent=session_percent,
        session_reset_text=session_reset,
        weekly_percent=weekly_percent,
        weekly_reset_text=weekly_reset,
        model_weekly=model_weekly,
    )


def parse_usage_response(
    payload: Mapping[str, Any],
    profile: Profile,
    *,
    plan: str | None = None,
) -> UsageSnapshot:
    """Validate and parse one JSON response from ``claude -p /usage``."""

    if not isinstance(payload, Mapping):
        raise UsageError("Claude returned an invalid usage response")
    if payload.get("local_command") != "usage":
        raise UsageError("Claude returned a non-local usage response")
    if payload.get("is_error") not in (None, False):
        raise UsageError("Claude reported a usage command error")

    # ``/usage`` is a local command.  A positive turn count or cost means the
    # response was produced by a model request instead, so do not present it
    # as an authoritative account quota snapshot.
    num_turns = payload.get("num_turns")
    if num_turns is not None and (
        isinstance(num_turns, bool)
        or not isinstance(num_turns, int)
        or num_turns < 0
        or num_turns > 0
    ):
        raise UsageError("Claude returned a non-local usage response")
    total_cost = payload.get("total_cost_usd")
    if total_cost is not None and (
        isinstance(total_cost, bool)
        or not isinstance(total_cost, (int, float))
        or total_cost < 0
        or total_cost > 0
    ):
        raise UsageError("Claude returned a non-local usage response")

    result = payload.get("result")
    if not isinstance(result, str):
        raise UsageError("Claude returned an invalid usage result")

    response_plan = payload.get("plan")
    if plan is None and isinstance(response_plan, str):
        plan = response_plan
    return parse_usage_text(result, profile, plan=plan)


def parse_usage_output(
    output: str | bytes | Mapping[str, Any],
    profile: Profile,
    *,
    plan: str | None = None,
) -> UsageSnapshot:
    """Decode and validate the JSON output of the usage command.

    This helper is public so callers and tests can parse a captured response
    without starting Claude.  Error messages deliberately omit the response
    body because it may contain account or workspace data.
    """

    if isinstance(output, Mapping):
        payload: object = output
    else:
        if isinstance(output, bytes):
            try:
                output = output.decode("utf-8")
            except UnicodeDecodeError:
                raise UsageError("Claude returned non-UTF-8 usage output") from None
        if not isinstance(output, str) or len(output) > _MAX_OUTPUT_CHARS:
            raise UsageError("Claude returned an invalid usage output")
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            raise UsageError("Claude returned invalid usage JSON") from None

    return parse_usage_response(payload, profile, plan=plan)  # type: ignore[arg-type]


def fetch_usage(
    profile: Profile,
    binary: str,
    timeout: float = 45,
    *,
    cancel_event: threading.Event | None = None,
) -> UsageSnapshot:
    """Fetch a usage snapshot using Claude's supported local command.

    ``auth_details`` is used for the display plan and to ensure a profile is
    signed in.  The actual usage call uses no session persistence, and all
    arguments are passed directly to a subprocess without a shell.
    """

    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")

    if cancel_event is not None and cancel_event.is_set():
        raise UsageError("usage check cancelled")

    try:
        status = (
            auth_details(profile, binary, cancel_event=cancel_event)
            if cancel_event is not None else auth_details(profile, binary)
        )
    except ProcessCancelled:
        raise UsageError("usage check cancelled") from None
    except (OSError, RuntimeError):
        raise UsageError("could not read Claude authentication status") from None
    if not isinstance(status, Mapping) or not status.get("loggedIn"):
        raise UsageError("Claude profile is not logged in")
    method = status.get("authMethod")
    provider = status.get("apiProvider")
    if method != "claude.ai" or provider not in (None, "firstParty"):
        raise UsageError("Claude profile does not use a supported claude.ai subscription")

    if cancel_event is not None and cancel_event.is_set():
        raise UsageError("usage check cancelled")

    try:
        command = [binary, "-p", "--no-session-persistence", "--output-format", "json", "/usage"]
        environment = profile_environment(profile)
        if cancel_event is None:
            result = subprocess.run(
                command, env=environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        else:
            result = run_cancelable(command, environment, timeout, cancel_event)
    except ProcessCancelled:
        raise UsageError("usage check cancelled") from None
    except subprocess.TimeoutExpired:
        raise UsageError("Claude usage command timed out") from None
    except OSError:
        raise UsageError("could not run Claude usage command") from None

    if result.returncode != 0:
        raise UsageError("Claude usage command failed")
    output = result.stdout
    if not isinstance(output, str) or len(output) > _MAX_OUTPUT_CHARS:
        raise UsageError("Claude returned an invalid usage output")
    return parse_usage_output(output, profile, plan=_plan_from_auth(status))


__all__ = [
    "UsageError",
    "UsageSnapshot",
    "fetch_usage",
    "parse_usage_output",
    "parse_usage_response",
    "parse_usage_text",
]
