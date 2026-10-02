"""Tests for live usage readings and burn-rate projection (live_usage.py)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ccshift import live_usage as lu

NOW = 1_800_000_000.0
WEEK = 7 * 86400.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def polled(five=(10.0, NOW + 3600), seven=(20.0, NOW + 3 * 86400)) -> dict:
    return {
        "five_hour": {"pct": five[0], "resets_at": iso(five[1])},
        "seven_day": {"pct": seven[0], "resets_at": iso(seven[1])},
    }


def reading(five=(30.0, NOW + 3600), seven=(25.0, NOW + 3 * 86400)) -> dict:
    out = {}
    if five:
        out["five_hour"] = {"pct": five[0], "resets_ts": five[1]}
    if seven:
        out["seven_day"] = {"pct": seven[0], "resets_ts": seven[1]}
    return out


class TestParseStatusline:
    def test_reads_both_windows(self):
        got = lu.parse_statusline(
            {
                "rate_limits": {
                    "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
                    "seven_day": {"used_percentage": 41, "resets_at": 1738857600},
                }
            }
        )
        assert got == {
            "five_hour": {"pct": 23.5, "resets_ts": 1738425600.0},
            "seven_day": {"pct": 41.0, "resets_ts": 1738857600.0},
        }

    @pytest.mark.parametrize(
        "payload",
        [None, [], {}, {"rate_limits": None}, {"rate_limits": {}},
         {"rate_limits": {"five_hour": {"used_percentage": "x"}}},
         {"rate_limits": {"five_hour": {"used_percentage": True}}},
         {"rate_limits": {"five_hour": {"used_percentage": float("nan")}}}],
    )
    def test_unusable_payload_is_none(self, payload):
        assert lu.parse_statusline(payload) is None

    def test_one_window_is_enough_and_pct_is_clamped(self):
        got = lu.parse_statusline(
            {"rate_limits": {"seven_day": {"used_percentage": 140}}}
        )
        assert got == {"seven_day": {"pct": 100.0, "resets_ts": None}}


class TestMatchAccount:
    def test_weekly_reset_picks_the_account(self):
        a = polled(seven=(20.0, NOW + 3 * 86400))
        b = polled(seven=(60.0, NOW + 5 * 86400))
        # 1s of rounding between the endpoint's ISO and the statusLine epoch.
        r = reading(seven=(25.0, NOW + 5 * 86400 + 1))
        assert lu.match_account(r, {"1": a, "2": b}, {}, NOW) == "2"

    def test_ambiguity_is_dropped_not_guessed(self):
        a = polled()
        b = polled()
        assert lu.match_account(reading(), {"1": a, "2": b}, {}, NOW) is None

    def test_unknown_window_is_dropped(self):
        a = polled(seven=(20.0, NOW + 3 * 86400))
        r = reading(five=None, seven=(25.0, NOW + 6 * 86400))
        assert lu.match_account(r, {"1": a}, {}, NOW) is None

    def test_falls_back_to_the_five_hour_window(self):
        a = polled(five=(10.0, NOW + 1800))
        b = polled(five=(10.0, NOW + 9000))
        r = reading(five=(40.0, NOW + 9000), seven=None)
        assert lu.match_account(r, {"1": a, "2": b}, {}, NOW) == "2"

    def test_weekly_window_that_rolled_since_the_last_poll(self):
        # Stored weekly reset is in the past: the new one is whole weeks on.
        stale = polled(seven=(90.0, NOW - 3600))
        other = polled(seven=(20.0, NOW + 2 * 86400))
        r = reading(five=None, seven=(2.0, NOW - 3600 + WEEK + 2))
        assert lu.match_account(r, {"1": stale, "2": other}, {}, NOW) == "1"

    def test_live_windows_count_as_references(self):
        live = {"seven_day": {"pct": 30.0, "resets_ts": NOW + 4 * 86400}}
        r = reading(five=None, seven=(35.0, NOW + 4 * 86400))
        assert lu.match_account(r, {"1": polled()}, {"1": live, "2": live}, NOW) is None
        assert lu.match_account(r, {"1": polled(), "2": None}, {"2": live}, NOW) == "2"


class TestAcceptWindow:
    def test_same_window_lower_pct_is_a_stale_resend(self):
        stored = {"pct": 50.0, "resets_ts": NOW + 100}
        incoming = {"pct": 40.0, "resets_ts": NOW + 100}
        assert lu.accept_window(stored, None, incoming) is None

    def test_same_window_higher_pct_is_taken(self):
        stored = {"pct": 50.0, "resets_ts": NOW + 100}
        incoming = {"pct": 55.0, "resets_ts": NOW + 100}
        assert lu.accept_window(stored, None, incoming) == incoming

    def test_a_later_window_replaces_an_earlier_one_even_at_lower_pct(self):
        stored = {"pct": 99.0, "resets_ts": NOW + 100}
        incoming = {"pct": 2.0, "resets_ts": NOW + 100 + 5 * 3600}
        assert lu.accept_window(stored, None, incoming) == incoming

    def test_an_earlier_window_is_stale(self):
        stored = {"pct": 5.0, "resets_ts": NOW + 5 * 3600}
        incoming = {"pct": 99.0, "resets_ts": NOW + 100}
        assert lu.accept_window(stored, None, incoming) is None

    def test_never_undercuts_a_fresher_poll(self):
        poll = {"pct": 70.0, "resets_at": iso(NOW + 100)}
        incoming = {"pct": 60.0, "resets_ts": NOW + 100}
        assert lu.accept_window(None, poll, incoming) is None


class TestMergeLive:
    def test_higher_live_pct_overlays_the_polled_window(self):
        merged, used = lu.merge_live(
            polled(), {"five_hour": {"pct": 80.0, "resets_ts": NOW + 3600}}
        )
        assert used is True
        assert merged["five_hour"]["pct"] == 80.0
        assert merged["seven_day"]["pct"] == 20.0  # untouched

    def test_lower_live_pct_never_wins(self):
        base = polled(five=(70.0, NOW + 3600))
        merged, used = lu.merge_live(
            base, {"five_hour": {"pct": 60.0, "resets_ts": NOW + 3600}}
        )
        assert used is False
        assert merged is base

    def test_a_new_window_wins_at_any_pct(self):
        merged, used = lu.merge_live(
            polled(five=(99.0, NOW - 60)),
            {"five_hour": {"pct": 1.0, "resets_ts": NOW + 5 * 3600}},
        )
        assert used is True
        assert merged["five_hour"]["pct"] == 1.0

    def test_scoped_windows_and_spend_are_untouched(self):
        base = {**polled(), "scoped": [{"name": "Fable", "pct": 3.0}]}
        merged, _ = lu.merge_live(
            base, {"five_hour": {"pct": 80.0, "resets_ts": NOW + 3600}}
        )
        assert merged["scoped"] == [{"name": "Fable", "pct": 3.0}]

    def test_live_alone_stands_when_nothing_was_polled(self):
        merged, used = lu.merge_live(
            None, {"five_hour": {"pct": 12.0, "resets_ts": NOW + 3600}}
        )
        assert used is True
        assert merged["five_hour"]["pct"] == 12.0

    def test_no_live_returns_the_polled_object(self):
        base = polled()
        assert lu.merge_live(base, None) == (base, False)


class TestBurnAndProjection:
    def test_update_burn_learns_a_rate_from_a_pair(self):
        burn = lu.update_burn(None, polled(five=(10.0, NOW + 3600)), NOW - 200,
                              polled(five=(20.0, NOW + 3600)), NOW)
        assert burn["five_hour"]["rate"] == pytest.approx(10.0 / 200)
        assert burn["five_hour"]["at"] == NOW

    def test_a_flat_pair_records_zero_so_old_bursts_stop_projecting(self):
        old = {"five_hour": {"rate": 0.05, "at": NOW - 500}}
        burn = lu.update_burn(old, polled(five=(20.0, NOW + 3600)), NOW - 200,
                              polled(five=(20.0, NOW + 3600)), NOW)
        assert burn["five_hour"]["rate"] == 0.0

    def test_rollover_and_tiny_gaps_teach_nothing(self):
        old = {"five_hour": {"rate": 0.01, "at": NOW - 10}}
        rolled = lu.update_burn(old, polled(five=(90.0, NOW - 10)), NOW - 200,
                                polled(five=(2.0, NOW + 18000)), NOW)
        # (the unchanged weekly window is a flat pair and records zero)
        assert rolled["five_hour"] == old["five_hour"]
        quick = lu.update_burn(old, polled(five=(10.0, NOW + 3600)), NOW - 5,
                               polled(five=(11.0, NOW + 3600)), NOW)
        assert quick == old

    def test_projection_advances_an_aging_reading(self):
        burn = {"five_hour": {"rate": 0.05, "at": NOW - 60}}
        got = lu.project_usage(polled(five=(60.0, NOW + 3600)), burn, 400.0, NOW)
        assert got["five_hour"]["pct"] == pytest.approx(60.0 + 0.05 * 400)
        assert got["seven_day"]["pct"] == 20.0

    def test_a_fresh_reading_is_not_projected(self):
        burn = {"five_hour": {"rate": 0.05, "at": NOW}}
        base = polled(five=(60.0, NOW + 3600))
        assert lu.project_usage(base, burn, 30.0, NOW) is base

    def test_projection_is_bounded_by_the_horizon_and_by_100(self):
        burn = {"five_hour": {"rate": 0.05, "at": NOW}}
        got = lu.project_usage(polled(five=(10.0, NOW + 3600)), burn, 5000.0, NOW)
        assert got["five_hour"]["pct"] == pytest.approx(
            10.0 + 0.05 * lu.PROJECTION_HORIZON_S
        )
        capped = lu.project_usage(polled(five=(90.0, NOW + 3600)), burn, 5000.0, NOW)
        assert capped["five_hour"]["pct"] == 100.0

    def test_a_stale_rate_or_a_rolled_window_is_not_projected(self):
        old = {"five_hour": {"rate": 0.05, "at": NOW - lu.BURN_MAX_AGE_S - 1}}
        base = polled(five=(60.0, NOW + 3600))
        assert lu.project_usage(base, old, 400.0, NOW) is base
        fresh = {"five_hour": {"rate": 0.05, "at": NOW}}
        rolled = polled(five=(60.0, NOW - 1))
        assert lu.project_usage(rolled, fresh, 400.0, NOW) is rolled
