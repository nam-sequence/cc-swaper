"""Per-window switch thresholds: the 5-hour and the 7-day limit each get one.

The engine reasons in one currency: the account's *binding* utilization
(``100 - headroom``) against ``settings.threshold``. Separate thresholds do not
need a second currency, only a change of units per window: each window's
percentage is re-expressed so that ITS OWN threshold lands on the base one.

    remap(p) = p * base / T                          p <= T
             = base + (p - T) * (100 - base) / (100 - T)   p >  T

Piecewise linear, monotone, fixing 0 and 100. So "the binding window is at the
base threshold" is exactly "some window is at its own threshold", and an
exhausted window (100%) stays exhausted, which the at-limit and all-exhausted
paths test with ``headroom <= 0``. Everything downstream — candidate
qualification ("below the threshold"), hysteresis, ranking, the urgent poll
band — then works unchanged on the re-expressed values, and with equal
thresholds the map is the identity, so nothing observable moves.

Display never reads re-expressed values: they are an internal unit, not a
percentage anyone should be shown.
"""

from __future__ import annotations

from dataclasses import dataclass

WINDOW_KEYS = ("five_hour", "seven_day")


@dataclass(frozen=True)
class WindowThresholds:
    """The switch point of each window, in percent used."""

    five_hour: float
    seven_day: float

    def for_label(self, label: str) -> float:
        """The threshold of a window by its display label.

        "5h" is the five-hour window. Everything else that gates an account —
        "7d" and the per-model weekly windows ("Fable", ...) — is weekly.
        """
        return self.five_hour if label == "5h" else self.seven_day

    def is_uniform(self, base: float) -> bool:
        return self.five_hour == base and self.seven_day == base


def remap_pct(pct: float, window_threshold: float, base: float) -> float:
    """``pct`` re-expressed so ``window_threshold`` maps onto ``base``."""
    if window_threshold == base:
        return pct
    if pct <= window_threshold:
        return pct * base / window_threshold
    return base + (pct - window_threshold) * (100.0 - base) / (100.0 - window_threshold)


def _remap_window(window: object, window_threshold: float, base: float) -> object:
    if not isinstance(window, dict):
        return window
    pct = window.get("pct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return window
    return {**window, "pct": remap_pct(float(pct), window_threshold, base)}


def normalize_usage(
    usage: object, thresholds: WindowThresholds, base: float
) -> object:
    """``usage`` with every gating window re-expressed against ``base``.

    Non-dict values (sentinel strings, ``None``) pass through, and so does a
    dict when the thresholds are uniform — the same object, so the common case
    costs nothing and cannot differ from before. ``spend`` is a separate axis
    the engine never reads and is left alone.
    """
    if not isinstance(usage, dict) or thresholds.is_uniform(base):
        return usage
    out = dict(usage)
    out["five_hour"] = _remap_window(usage.get("five_hour"), thresholds.five_hour, base)
    out["seven_day"] = _remap_window(usage.get("seven_day"), thresholds.seven_day, base)
    scoped = usage.get("scoped")
    if isinstance(scoped, list):
        out["scoped"] = [
            _remap_window(s, thresholds.seven_day, base) for s in scoped
        ]
    for key in ("five_hour", "seven_day"):
        if key not in usage:
            out.pop(key, None)
    return out
