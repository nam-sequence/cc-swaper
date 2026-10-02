"""Engine behaviour around blind readings and rate-limit reports."""

from __future__ import annotations

import pytest

from ccshift.autoswitch import (
    ACTIVE_BLIND_S,
    LIMIT_HIT_GRACE_S,
    NoSwitchEvent,
    SwitchEvent,
    TickOutcome,
)
from ccshift.usage_store import UsageEntry

from tests.test_autoswitch import EngineHarness, _usage, harness  # noqa: F401


def _blind_entry(pct: float, age_s: float) -> UsageEntry:
    """A polled reading frozen by a 429 block: old, and still decision-trusted."""
    return UsageEntry(
        last_good=_usage(pct),
        fetched_at=1_000_000.0 - age_s,
        age_s=age_s,
        last_error="http-429",
        consecutive_failures=1,
        trust_extended=True,
    )


def _fresh(pct: float) -> UsageEntry:
    return UsageEntry(last_good=_usage(pct), fetched_at=1_000_000.0, age_s=0.0)


class TestBlindActiveAccount:
    def test_frozen_reading_in_the_danger_band_stops_counting(self, harness):
        entries = {"1": _blind_entry(80, ACTIVE_BLIND_S + 60), "2": _fresh(10), "3": _fresh(20)}
        outcomes = []
        for _ in range(harness.settings.unhealthy_ticks):
            outcomes.append(harness.tick_with_entries(entries))
        assert outcomes[:-1] == [TickOutcome.NO_ACTION] * (len(outcomes) - 1)
        assert outcomes[-1] is TickOutcome.SWITCHED
        switch = next(e for e in harness.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "failover"
        assert harness.active_number() == 2
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons[0] == "active-usage-unknown"

    def test_frozen_reading_far_from_the_limit_is_still_the_best_evidence(self, harness):
        entries = {"1": _blind_entry(30, ACTIVE_BLIND_S + 60), "2": _fresh(10), "3": _fresh(20)}
        assert harness.tick_with_entries(entries) is TickOutcome.NO_ACTION
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_a_reading_inside_the_blind_window_is_trusted(self, harness):
        entries = {"1": _blind_entry(80, ACTIVE_BLIND_S - 60), "2": _fresh(10), "3": _fresh(20)}
        assert harness.tick_with_entries(entries) is TickOutcome.NO_ACTION
        reasons = [e.reason for e in harness.events if isinstance(e, NoSwitchEvent)]
        assert reasons == ["below-threshold"]

    def test_an_old_reading_with_no_fetch_trouble_is_not_blind(self, harness):
        # Old because the planner chose it (no error): the existing trust
        # rules own that case.
        quiet = UsageEntry(
            last_good=_usage(80), fetched_at=1.0, age_s=ACTIVE_BLIND_S + 60,
            trust_extended=True,
        )
        entries = {"1": quiet, "2": _fresh(10), "3": _fresh(20)}
        assert harness.tick_with_entries(entries) is TickOutcome.NO_ACTION


class TestLimitHit:
    @pytest.fixture
    def hit(self, harness):
        harness.engine = harness._make_engine(limit_hit=True)
        return harness

    def test_switches_even_though_the_reading_says_there_is_room(self, hit):
        outcome = hit.tick_with_usage({"1": _usage(40), "2": _usage(10), "3": _usage(20)})
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in hit.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"
        assert hit.active_number() == 2

    def test_bypasses_the_cooldown_like_any_at_limit_switch(self, hit):
        hit.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(20)})
        hit.make_live("b@example.com", 2)
        hit.engine = hit._make_engine(limit_hit=True)
        hit.clock.advance(LIMIT_HIT_GRACE_S + 5)  # past grace, well inside cooldown
        outcome = hit.tick_with_usage({"1": _usage(10), "2": _usage(40), "3": _usage(20)})
        assert outcome is TickOutcome.SWITCHED

    def test_a_report_that_predates_our_own_switch_is_ignored(self, hit):
        hit.tick_with_usage({"1": _usage(95), "2": _usage(10), "3": _usage(20)})
        hit.make_live("b@example.com", 2)
        hit.engine = hit._make_engine(limit_hit=True)
        hit.clock.advance(LIMIT_HIT_GRACE_S - 10)
        before = hit.active_number()
        outcome = hit.tick_with_usage({"1": _usage(10), "2": _usage(40), "3": _usage(20)})
        assert outcome is TickOutcome.NO_ACTION
        assert hit.active_number() == before
        reasons = [e.reason for e in hit.events if isinstance(e, NoSwitchEvent)]
        assert reasons[-1] == "limit-hit-grace"

    def test_it_is_one_shot(self, hit):
        hit.tick_with_usage({"1": _usage(40), "2": _usage(10), "3": _usage(20)})
        hit.make_live("b@example.com", 2)
        hit.clock.advance(3600)
        hit.events.clear()
        outcome = hit.tick_with_usage({"1": _usage(40), "2": _usage(30), "3": _usage(20)})
        assert outcome is TickOutcome.NO_ACTION
