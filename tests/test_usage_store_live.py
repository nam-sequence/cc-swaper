"""The usage store's live (statusLine-fed) overlay and burn rate."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ccshift import poll_policy
from ccshift.usage_store import FetchRecord, UsageStore

IDENT = {"1": ("a@x.com", "org"), "2": ("b@x.com", "org")}
T0 = 1_800_000_000.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def usage(five: float, seven: float, five_reset: float, seven_reset: float) -> dict:
    return {
        "five_hour": {"pct": five, "resets_at": iso(five_reset)},
        "seven_day": {"pct": seven, "resets_at": iso(seven_reset)},
    }


def reading(five: float, five_reset: float, seven: float, seven_reset: float) -> dict:
    return {
        "five_hour": {"pct": five, "resets_ts": five_reset},
        "seven_day": {"pct": seven, "resets_ts": seven_reset},
    }


class Clock:
    def __init__(self, now: float = T0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = UsageStore(tmp_path / "cache", clock=clock)
    # Two accounts, distinguishable by their weekly resets.
    s.record(
        {
            "1": FetchRecord(usage=usage(20, 30, T0 + 3600, T0 + 3 * 86400)),
            "2": FetchRecord(usage=usage(50, 60, T0 + 7200, T0 + 5 * 86400)),
        },
        IDENT,
    )
    return s


class TestFeedLive:
    def test_reading_lands_on_the_account_whose_weekly_reset_matches(self, store, clock):
        clock.now += 60
        slot = store.feed_live(
            reading(55, T0 + 7200, 61, T0 + 5 * 86400 + 1), IDENT
        )
        assert slot == "2"
        entries = store.entries(IDENT)
        assert entries["2"].last_good["five_hour"]["pct"] == 55
        assert entries["1"].last_good["five_hour"]["pct"] == 20

    def test_unattributable_reading_is_dropped(self, store):
        assert store.feed_live(reading(55, T0 + 1, 61, T0 + 9 * 86400), IDENT) is None
        assert store.entries(IDENT)["2"].live_at is None

    def test_overlay_is_fresher_than_the_poll_but_the_poll_stays_the_poll(
        self, store, clock
    ):
        clock.now += 400  # the poll is now past STALE_OK_S
        assert store.entries(IDENT)["2"].decision_value() is None
        store.feed_live(reading(58, T0 + 7200, 61, T0 + 5 * 86400), IDENT)
        entry = store.entries(IDENT)["2"]
        assert entry.age_s == 0.0
        assert entry.poll_age_s == pytest.approx(400.0)
        assert entry.polled_usage["five_hour"]["pct"] == 50  # raw, un-overlaid
        assert entry.decision_value()["five_hour"]["pct"] == 58

    def test_a_stale_resend_is_not_news_and_does_not_write(self, store, clock):
        clock.now += 30
        store.feed_live(reading(58, T0 + 7200, 61, T0 + 5 * 86400), IDENT)
        before = store.path.read_text(encoding="utf-8")
        clock.now += 30
        again = store.feed_live(reading(58, T0 + 7200, 61, T0 + 5 * 86400), IDENT)
        lower = store.feed_live(reading(55, T0 + 7200, 61, T0 + 5 * 86400), IDENT)
        assert again is None and lower is None
        assert store.path.read_text(encoding="utf-8") == before
        assert store.entries(IDENT)["2"].live_at == T0 + 30

    def test_a_reading_that_only_restates_the_poll_is_not_news(self, store, clock):
        clock.now += 30
        assert (
            store.feed_live(reading(50, T0 + 7200, 60, T0 + 5 * 86400), IDENT)
            is None
        )

    def test_a_later_poll_that_is_higher_wins_over_the_overlay(self, store, clock):
        clock.now += 30
        store.feed_live(reading(58, T0 + 7200, 61, T0 + 5 * 86400), IDENT)
        clock.now += 30
        store.record(
            {"2": FetchRecord(usage=usage(70, 62, T0 + 7200, T0 + 5 * 86400))}, IDENT
        )
        entry = store.entries(IDENT)["2"]
        assert entry.last_good["five_hour"]["pct"] == 70

    def test_feed_never_touches_fetch_state_or_the_poll_plan(self, store, clock):
        store.set_poll_plan({"2": (T0 + 500, 180.0)}, IDENT)
        clock.now += 30
        store.feed_live(reading(58, T0 + 7200, 61, T0 + 5 * 86400), IDENT)
        row = json.loads(store.path.read_text(encoding="utf-8"))["accounts"]["2"]
        assert row["lastGood"]["five_hour"]["pct"] == 50
        assert row["fetchedAt"] == T0
        assert row["nextPollAt"] == T0 + 500
        assert row["pollIntervalS"] == 180.0

    def test_the_planner_sees_polls_not_the_feed(self, store, clock):
        # The feed already saw 50 -> 58; the next poll reads 58. Compared
        # against the overlaid view that is "no movement" and slows the
        # cadence of an account that is burning; against the polled view it
        # is the 8-point jump it is.
        clock.now += 60
        store.feed_live(reading(58, T0 + 7200, 61, T0 + 5 * 86400), IDENT)
        entry = store.entries(IDENT)["2"]
        _, interval = poll_policy.plan_after_fetch(
            prev_interval_s=300.0,
            prev_usage=entry.polled_usage,
            new_usage=usage(58, 61, T0 + 7200, T0 + 5 * 86400),
            is_active=True,
            threshold=90.0,
            models=(),
            recent_429=False,
            now=clock.now,
            rng=lambda: 0.5,
        )
        assert interval == poll_policy.MIN_INTERVAL_S


class TestBurnProjection:
    def test_feed_samples_teach_a_rate_and_an_aging_reading_is_projected(
        self, store, clock
    ):
        clock.now += 100
        store.feed_live(reading(60, T0 + 7200, 61, T0 + 5 * 86400), IDENT)
        clock.now += 100
        store.feed_live(reading(70, T0 + 7200, 61, T0 + 5 * 86400), IDENT)
        # Then the feed goes quiet and the poll is blocked: the reading ages.
        clock.now += 300
        entry = store.entries(IDENT)["2"]
        assert entry.burn["five_hour"]["rate"] == pytest.approx(10 / 100)
        assert entry.age_s == pytest.approx(300.0)
        assert entry.decision_value()["five_hour"]["pct"] == pytest.approx(
            70 + 0.1 * 300
        )

    def test_a_poll_pair_teaches_a_rate_too(self, store, clock):
        clock.now += 200
        store.record(
            {"2": FetchRecord(usage=usage(60, 60, T0 + 7200, T0 + 5 * 86400))}, IDENT
        )
        entry = store.entries(IDENT)["2"]
        assert entry.burn["five_hour"]["rate"] == pytest.approx(10 / 200)

    def test_no_rate_no_projection(self, store, clock):
        clock.now += 250
        entry = store.entries(IDENT)["2"]
        assert entry.burn is None
        assert entry.decision_value() == entry.last_good


class TestAPollSupersedesOlderLiveReadings:
    """Regression: quota reset early (or re-granted) inside the same window.

    The poll reads LOW, an older live reading says HIGH. "Usage only rises" is
    false there, and the live number must not outlive the poll that contradicts
    it — it used to, so a refresh showed (and the engine acted on) 99% for an
    account whose usage had been reset to 0.
    """

    def test_a_reset_read_by_the_poll_wins_over_an_older_live_reading(self, store, clock):
        clock.now += 60
        store.feed_live(reading(70, T0 + 7200, 99, T0 + 5 * 86400), IDENT)
        assert store.entries(IDENT)["2"].last_good["seven_day"]["pct"] == 99
        clock.now += 60
        # Same windows (same resets_at), but the provider reset the usage.
        store.record(
            {"2": FetchRecord(usage=usage(0, 0, T0 + 7200, T0 + 5 * 86400))}, IDENT
        )
        entry = store.entries(IDENT)["2"]
        assert entry.last_good["five_hour"]["pct"] == 0
        assert entry.last_good["seven_day"]["pct"] == 0
        assert entry.decision_value()["seven_day"]["pct"] == 0
        assert entry.live_at is None

    def test_a_poll_that_reports_no_window_beats_an_older_live_window(self, store, clock):
        clock.now += 60
        store.feed_live(reading(64, T0 + 7200, 61, T0 + 5 * 86400), IDENT)
        clock.now += 60
        store.record(
            {
                "2": FetchRecord(
                    usage={
                        "five_hour": {"pct": 0.0},  # idle: no resets_at
                        "seven_day": {
                            "pct": 61.0,
                            "resets_at": iso(T0 + 5 * 86400),
                        },
                    }
                )
            },
            IDENT,
        )
        assert store.entries(IDENT)["2"].last_good["five_hour"]["pct"] == 0.0

    def test_a_live_window_that_has_rolled_over_is_dropped(self, store, clock):
        clock.now += 60
        store.feed_live(reading(70, T0 + 7200, 61, T0 + 5 * 86400), IDENT)
        assert store.entries(IDENT)["2"].last_good["five_hour"]["pct"] == 70
        clock.now = T0 + 7200 + 1  # past the 5h reset; nobody has polled since
        entry = store.entries(IDENT)["2"]
        assert entry.last_good["five_hour"]["pct"] == 50, "falls back to the poll"

    def test_a_reading_after_the_poll_is_taken_even_when_it_is_below_the_old_live_one(
        self, store, clock
    ):
        clock.now += 60
        store.feed_live(reading(70, T0 + 7200, 99, T0 + 5 * 86400), IDENT)
        clock.now += 60
        store.record(
            {"2": FetchRecord(usage=usage(0, 0, T0 + 7200, T0 + 5 * 86400))}, IDENT
        )
        clock.now += 30
        # The reset account is used again: 3% / 2%, far below the stale 99.
        assert store.feed_live(reading(3, T0 + 7200, 2, T0 + 5 * 86400), IDENT) == "2"
        entry = store.entries(IDENT)["2"]
        assert entry.last_good["five_hour"]["pct"] == 3
        assert entry.last_good["seven_day"]["pct"] == 2

    def test_old_live_windows_do_not_ride_along_with_a_new_reading(self, store, clock):
        clock.now += 60
        store.feed_live(reading(70, T0 + 7200, 99, T0 + 5 * 86400), IDENT)
        clock.now += 60
        store.record(
            {"2": FetchRecord(usage=usage(0, 0, T0 + 7200, T0 + 5 * 86400))}, IDENT
        )
        clock.now += 30
        # Only the 5h window arrives; the stale 7d=99 must not come back with it.
        slot = store.feed_live({"five_hour": {"pct": 3.0, "resets_ts": T0 + 7200}}, IDENT)
        assert slot == "2"
        entry = store.entries(IDENT)["2"]
        assert entry.last_good["five_hour"]["pct"] == 3
        assert entry.last_good["seven_day"]["pct"] == 0
