"""Per-window switch thresholds: the unit change, the settings, the flags."""

from __future__ import annotations

import argparse

import pytest

from ccshift import oauth
from ccshift.exceptions import ConfigError
from ccshift.settings import (
    AutoSwitchSettings,
    effective_settings,
    load_settings,
    merged_with_cli,
    parse_setting_value,
    save_settings,
    set_setting,
    setting_spec,
    unset_setting,
)
from ccshift.thresholds import WindowThresholds, normalize_usage, remap_pct


class TestRemap:
    def test_a_windows_own_threshold_lands_on_the_base(self):
        assert remap_pct(80.0, 80.0, 90.0) == pytest.approx(90.0)
        assert remap_pct(95.0, 95.0, 90.0) == pytest.approx(90.0)

    def test_zero_and_exhaustion_are_fixed_points(self):
        for window in (55.0, 80.0, 95.0, 99.9):
            assert remap_pct(0.0, window, 90.0) == 0.0
            assert remap_pct(100.0, window, 90.0) == pytest.approx(100.0)

    def test_monotone_and_continuous_across_the_threshold(self):
        for window in (60.0, 80.0, 95.0):
            values = [remap_pct(p / 2, window, 90.0) for p in range(0, 201)]
            assert values == sorted(values)
            below, above = remap_pct(window - 1e-9, window, 90.0), remap_pct(
                window + 1e-9, window, 90.0
            )
            assert above - below < 1e-6

    def test_equal_thresholds_are_the_identity(self):
        assert remap_pct(42.5, 90.0, 90.0) == 42.5


def _usage(five=10.0, seven=10.0, scoped=None) -> dict:
    out = {"five_hour": {"pct": five}, "seven_day": {"pct": seven}}
    if scoped is not None:
        out["scoped"] = [{"name": "Fable", "pct": scoped}]
    return out


class TestNormalizeUsage:
    def test_uniform_thresholds_return_the_same_object(self):
        usage = _usage(50, 60)
        assert normalize_usage(usage, WindowThresholds(90, 90), 90.0) is usage

    def test_each_window_is_measured_against_its_own_limit(self):
        limits = WindowThresholds(five_hour=80.0, seven_day=95.0)
        out = normalize_usage(_usage(80, 95), limits, 90.0)
        assert out["five_hour"]["pct"] == pytest.approx(90.0)
        assert out["seven_day"]["pct"] == pytest.approx(90.0)
        # Headroom through the real function: both windows sit exactly on the
        # switch point, so the binding utilization is the base threshold.
        assert oauth.account_headroom(out) == pytest.approx(10.0)

    def test_the_binding_window_is_the_one_nearest_its_own_limit(self):
        limits = WindowThresholds(five_hour=80.0, seven_day=95.0)
        # 7d is numerically higher but farther from ITS limit; 5h binds.
        out = normalize_usage(_usage(75, 85), limits, 90.0)
        assert oauth.account_headroom(out) == pytest.approx(100 - 75 * 90 / 80)

    def test_per_model_weekly_windows_follow_the_weekly_limit(self):
        limits = WindowThresholds(five_hour=80.0, seven_day=95.0)
        out = normalize_usage(_usage(1, 1, scoped=95.0), limits, 90.0)
        assert out["scoped"][0]["pct"] == pytest.approx(90.0)
        assert out["scoped"][0]["name"] == "Fable"

    def test_input_is_not_mutated_and_other_keys_survive(self):
        usage = {**_usage(80, 40), "spend": {"pct": 12.0}}
        before = {k: dict(v) for k, v in usage.items()}
        out = normalize_usage(usage, WindowThresholds(70.0, 95.0), 90.0)
        assert usage == before
        assert out["spend"] == {"pct": 12.0}

    @pytest.mark.parametrize("value", [None, "token expired", "api key"])
    def test_sentinels_and_unknowns_pass_through(self, value):
        assert normalize_usage(value, WindowThresholds(70.0, 95.0), 90.0) is value

    def test_a_missing_window_stays_missing(self):
        out = normalize_usage({"five_hour": {"pct": 50.0}}, WindowThresholds(70.0, 95.0), 90.0)
        assert "seven_day" not in out

    def test_exhausted_stays_exhausted(self):
        out = normalize_usage(_usage(100, 5), WindowThresholds(80.0, 95.0), 90.0)
        assert oauth.account_headroom(out) == pytest.approx(0.0)


class TestSettings:
    def test_unset_windows_follow_threshold(self):
        s = AutoSwitchSettings(threshold=85.0)
        assert s.window_thresholds() == WindowThresholds(85.0, 85.0)

    def test_either_window_can_be_set_alone(self):
        s = AutoSwitchSettings(threshold=85.0, seven_day_threshold=95.0)
        assert s.window_thresholds() == WindowThresholds(85.0, 95.0)

    def test_set_get_unset_round_trip(self, tmp_path):
        assert set_setting(tmp_path, "autoswitch.fiveHourThreshold", "80") == 80.0
        set_setting(tmp_path, "autoswitch.sevenDayThreshold", "95.5")
        s = load_settings(tmp_path)
        assert (s.five_hour_threshold, s.seven_day_threshold) == (80.0, 95.5)
        assert unset_setting(tmp_path, "autoswitch.fiveHourThreshold") is True
        assert load_settings(tmp_path).five_hour_threshold is None
        assert load_settings(tmp_path).seven_day_threshold == 95.5

    @pytest.mark.parametrize("bad", ["49", "100", "abc", ""])
    def test_strict_validation_on_set(self, bad):
        with pytest.raises(ConfigError):
            parse_setting_value(setting_spec("autoswitch.sevenDayThreshold"), bad)

    def test_hand_edited_garbage_degrades_to_unset_and_out_of_range_clamps(self, tmp_path):
        (tmp_path / "settings.json").write_text(
            '{"autoswitch": {"fiveHourThreshold": "x", "sevenDayThreshold": 150}}'
        )
        s = load_settings(tmp_path)
        assert s.five_hour_threshold is None
        assert s.seven_day_threshold == 99.9

    def test_config_list_shows_the_value_in_force_and_marks_it_default(self, tmp_path):
        set_setting(tmp_path, "autoswitch.threshold", "80")
        rows = {spec.dotted: (value, is_set) for spec, value, is_set in effective_settings(tmp_path)}
        assert rows["autoswitch.fiveHourThreshold"] == (80.0, False)
        assert rows["autoswitch.sevenDayThreshold"] == (80.0, False)

    def test_save_settings_never_writes_a_null_for_an_unset_window(self, tmp_path):
        save_settings(tmp_path, AutoSwitchSettings(seven_day_threshold=95.0))
        import json

        section = json.loads((tmp_path / "settings.json").read_text())["autoswitch"]
        assert section["sevenDayThreshold"] == 95.0
        assert "fiveHourThreshold" not in section


class TestCliMerge:
    def args(self, **kw):
        base = dict(
            threshold=None, threshold_5h=None, threshold_7d=None, interval=None,
            cooldown=None, include_api_key_accounts=None, model=None, strategy=None,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def test_a_per_window_flag_overrides_one_window(self):
        s = merged_with_cli(AutoSwitchSettings(), self.args(threshold_5h=75.0))
        assert s.window_thresholds() == WindowThresholds(75.0, 90.0)

    def test_threshold_flag_means_both_windows_and_outranks_the_file(self):
        file_settings = AutoSwitchSettings(five_hour_threshold=70.0, seven_day_threshold=95.0)
        s = merged_with_cli(file_settings, self.args(threshold=80.0))
        assert s.window_thresholds() == WindowThresholds(80.0, 80.0)

    def test_a_per_window_flag_outranks_the_threshold_flag(self):
        s = merged_with_cli(
            AutoSwitchSettings(), self.args(threshold=80.0, threshold_7d=95.0)
        )
        assert s.window_thresholds() == WindowThresholds(80.0, 95.0)

    def test_no_flags_leaves_the_file_alone(self):
        file_settings = AutoSwitchSettings(five_hour_threshold=70.0)
        assert merged_with_cli(file_settings, self.args()) is file_settings
