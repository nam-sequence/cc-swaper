"""A person pressing refresh fetches now — but never past a Retry-After."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from ccshift import cli
from ccshift.usage_store import (
    AUTH_DEAD_STRIKES,
    FORCE_MIN_AGE_S,
    SERVE_TTL_S,
    FetchRecord,
    UsageStore,
)

IDENT = {"1": ("a@x.com", "org")}
USAGE = {"five_hour": {"pct": 25.0}, "seven_day": {"pct": 10.0}}


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = UsageStore(tmp_path / "cache", clock=clock)
    s.record({"1": FetchRecord(usage=USAGE)}, IDENT)
    return s


def planned(store, clock, *, due_in: float):
    store.set_poll_plan({"1": (clock.now + due_in, 300.0)}, IDENT)


class TestReserveForce:
    def test_a_click_fetches_what_the_plan_would_wait_on(self, store, clock):
        planned(store, clock, due_in=240.0)
        clock.now += SERVE_TTL_S + 5  # stale, but the plan says wait
        assert store.reserve(["1"], IDENT, respect_plans=True) == {}
        assert "1" in store.reserve(["1"], IDENT, respect_plans=True, force=True)

    def test_a_click_fetches_even_inside_the_serve_ttl(self, store, clock):
        planned(store, clock, due_in=240.0)
        clock.now += FORCE_MIN_AGE_S + 1
        assert "1" in store.reserve(["1"], IDENT, respect_plans=True, force=True)

    def test_a_reading_younger_than_the_floor_is_left_alone(self, store, clock):
        clock.now += FORCE_MIN_AGE_S - 1
        assert store.reserve(["1"], IDENT, respect_plans=True, force=True) == {}

    def test_a_never_fetched_account_is_fetched(self, tmp_path, clock):
        empty = UsageStore(tmp_path / "other", clock=clock)
        assert "1" in empty.reserve(["1"], IDENT, respect_plans=True, force=True)

    def test_a_retry_after_is_never_defeated(self, store, clock):
        store.record(
            {"1": FetchRecord(error="http-429", retry_after_s=3600.0)}, IDENT
        )
        clock.now += 300
        assert store.reserve(["1"], IDENT, respect_plans=True, force=True) == {}

    def test_a_live_lease_still_holds(self, store, clock):
        clock.now += 60
        store.reserve(["1"], IDENT, respect_plans=True, force=True)
        assert store.reserve(["1"], IDENT, respect_plans=True, force=True) == {}

    def test_a_hold_for_another_machines_reading_still_holds(self, store, clock):
        clock.now += 60
        store.adopt({"1": (USAGE, 120.0)}, IDENT, hold_s=600.0)
        assert store.reserve(["1"], IDENT, respect_plans=True, force=True) == {}

    def test_a_dead_token_still_holds(self, store, clock):
        clock.now += 60
        for _ in range(AUTH_DEAD_STRIKES):
            store.record(
                {"1": FetchRecord(error="invalid_grant", struck_fp="fp")}, IDENT
            )
        clock.now += 3600
        assert store.reserve(["1"], IDENT, respect_plans=True, force=True) == {}


class TestCliRefresh:
    def run(self, *argv):
        with patch("ccshift.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["ccshift", *argv]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("ccshift.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.list_accounts.return_value = {
                "schemaVersion": 1, "accounts": [],
            }
            cli.main()
        return switcher_cls.return_value.list_accounts

    def test_list_refresh_forces_the_fetch(self):
        self.run("list", "--json", "--refresh").assert_called_once_with(
            show_token_status=False, json_output=True, force=True,
        )

    def test_a_plain_list_does_not(self):
        self.run("list", "--json").assert_called_once_with(
            show_token_status=False, json_output=True,
        )

    @pytest.mark.parametrize(
        "argv",
        [["status", "--refresh"], ["list", "--refresh", "--cached"]],
    )
    def test_misuse_is_rejected(self, argv):
        with patch("ccshift.cli.ClaudeAccountSwitcher"), \
             patch.object(sys, "argv", ["ccshift", *argv]), \
             patch("os.geteuid", return_value=1000, create=True), \
             pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
