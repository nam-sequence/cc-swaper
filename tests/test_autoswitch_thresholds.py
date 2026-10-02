"""The engine with a separate switch point per window."""

from __future__ import annotations

import pytest

from ccshift.autoswitch import (
    NoSwitchEvent,
    PollEvent,
    SwitchEvent,
    TickOutcome,
)
from ccshift.thresholds import WindowThresholds
from ccshift.usage_store import FetchRecord, UsageEntry

from tests.test_autoswitch import EngineHarness, harness  # noqa: F401


def _u(five: float, seven: float) -> dict:
    return {"five_hour": {"pct": five}, "seven_day": {"pct": seven}}


@pytest.fixture
def split(temp_home):
    """5h switches at 80, 7d at 95 (the default threshold 90 sits between)."""
    h = EngineHarness(
        temp_home, five_hour_threshold=80.0, seven_day_threshold=95.0
    )
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.seed(3, "c@example.com")
    h.make_live("a@example.com", 1)
    return h


class TestEachWindowSwitchesAtItsOwnLimit:
    def test_five_hour_past_its_limit_switches_though_under_the_default(self, split):
        outcome = split.tick_with_usage({"1": _u(82, 10), "2": _u(10, 10), "3": _u(20, 10)})
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in split.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "proactive"

    def test_seven_day_under_its_limit_does_not_switch_though_over_the_default(self, split):
        outcome = split.tick_with_usage({"1": _u(10, 93), "2": _u(10, 10), "3": _u(20, 10)})
        assert outcome is TickOutcome.NO_ACTION
        assert split.active_number() == 1

    def test_seven_day_past_its_limit_switches(self, split):
        outcome = split.tick_with_usage({"1": _u(10, 96), "2": _u(10, 10), "3": _u(20, 10)})
        assert outcome is TickOutcome.SWITCHED

    def test_the_hold_names_the_window_and_its_real_numbers(self, split):
        split.tick_with_usage({"1": _u(70, 40), "2": _u(10, 10), "3": _u(10, 10)})
        detail = next(
            e.detail for e in split.events
            if isinstance(e, NoSwitchEvent) and e.reason == "below-threshold"
        )
        assert detail == "5h 70% < 80%"

    def test_the_hold_picks_the_window_nearest_its_own_limit(self, split):
        split.tick_with_usage({"1": _u(60, 93), "2": _u(10, 10), "3": _u(10, 10)})
        detail = next(
            e.detail for e in split.events
            if isinstance(e, NoSwitchEvent) and e.reason == "below-threshold"
        )
        assert detail == "7d 93% < 95%"

    def test_a_target_must_be_below_BOTH_of_its_own_limits(self, split):
        # Account 2: 5h at 85 is over the 5h limit (80) though under the
        # default (90); account 3 is clean. The engine must land on 3.
        outcome = split.tick_with_usage({"1": _u(82, 10), "2": _u(85, 5), "3": _u(40, 40)})
        assert outcome is TickOutcome.SWITCHED
        assert split.active_number() == 3

    def test_exhaustion_is_still_exhaustion(self, split):
        outcome = split.tick_with_usage({"1": _u(10, 100), "2": _u(10, 10), "3": _u(20, 10)})
        assert outcome is TickOutcome.SWITCHED
        switch = next(e for e in split.events if isinstance(e, SwitchEvent))
        assert switch.trigger == "at-limit"


class TestUniformThresholdsAreUntouched:
    def test_no_thresholds_field_and_the_legacy_detail(self, harness):  # noqa: F811
        harness.tick_with_usage({"1": _u(50, 20), "2": _u(10, 10), "3": _u(10, 10)})
        poll = next(e for e in harness.events if isinstance(e, PollEvent))
        assert poll.thresholds == {}
        assert "thresholds" not in poll.to_json()
        detail = next(
            e.detail for e in harness.events
            if isinstance(e, NoSwitchEvent) and e.reason == "below-threshold"
        )
        assert detail == "50% < 90%"


class TestEvents:
    def test_poll_event_reports_real_percentages_and_each_limit(self, split):
        split.tick_with_usage({"1": _u(70, 40), "2": _u(10, 10), "3": _u(10, 10)})
        poll = next(e for e in split.events if isinstance(e, PollEvent))
        assert poll.windows["1"] == {"5h": 70.0, "7d": 40.0}
        assert poll.thresholds == {"5h": 80.0, "7d": 95.0}
        assert poll.headroom["1"] == pytest.approx(30.0), "real headroom, not the engine's unit"
        assert poll.to_json()["thresholds"] == {"5h": 80.0, "7d": 95.0}
        assert poll.threshold == 90.0
        text = poll.human()
        assert "5h 70% · 7d 40%" in text
        assert "switch at 5h 80% · 7d 95%" in text


class TestSessionOverride:
    def test_apply_threshold_is_one_number_for_both_windows(self, split):
        split.engine.apply_threshold(70.0)
        assert split.settings is not split.engine.settings
        assert split.engine.settings.window_thresholds() == WindowThresholds(70.0, 70.0)
        assert split.switcher._poll_window_thresholds() == WindowThresholds(70.0, 70.0)


class TestPollPlanner:
    def test_the_urgent_band_is_judged_against_each_windows_own_limit(self, split):
        split.switcher.set_poll_policy_inputs(
            90.0, (), WindowThresholds(five_hour=70.0, seven_day=95.0)
        )
        info = {"1": (1, "a@example.com", "", "", True, "", "")}
        pre = {"1": UsageEntry(last_good=_u(50, 10), fetched_at=1.0, age_s=0.0, poll_interval_s=180.0)}
        records = {"1": FetchRecord(usage=_u(60, 10))}
        (_, interval), = split.switcher._plans_after_fetch(records, pre, info).values()
        # 5h 50 -> 60 is under 75 on the default scale but past 77 on its own
        # (limit 70), so the active account polls at the urgent cadence.
        assert interval == 60.0

    def test_with_one_threshold_the_same_movement_is_not_urgent(self, split):
        split.switcher.set_poll_policy_inputs(90.0, (), WindowThresholds(90.0, 90.0))
        info = {"1": (1, "a@example.com", "", "", True, "", "")}
        pre = {"1": UsageEntry(last_good=_u(50, 10), fetched_at=1.0, age_s=0.0, poll_interval_s=180.0)}
        records = {"1": FetchRecord(usage=_u(60, 10))}
        (_, interval), = split.switcher._plans_after_fetch(records, pre, info).values()
        assert interval > 60.0
