"""Live usage readings and burn-rate projection.

``/api/oauth/usage`` is the only *pollable* usage source and it is budgeted
(see ``poll_policy``), so a polled reading is minutes old at best and, after a
429, up to an hour and a quarter old. Claude Code itself already knows the
current numbers: every API response carries the 5h/7d utilization, and it
hands them to the configured ``statusLine`` command as
``rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}`` — free, with
no request budget, as fresh as the last response of any running session.

This module is the pure half of feeding that into the usage store:

* :func:`parse_statusline` — the statusLine JSON → live windows;
* :func:`match_account` — which managed account a reading belongs to. The
  payload carries no identity, and after a switch a still-running session can
  hold the *previous* account's token for a while, so the reading is matched
  by its window reset instants (the weekly one is effectively unique per
  account) instead of being credited to whichever account is active;
* :func:`merge_live` — overlay live windows on a polled usage dict;
* :func:`update_burn` / :func:`project_usage` — a burn rate learned from
  consecutive readings, used to extrapolate a reading that is aging (a 429
  block, a slow plan) instead of trusting it as if it were current.

A live reading never replaces the polled ``lastGood``: the poll planner
detects movement by comparing consecutive *polled* readings, and a feed that
pre-empted that comparison would make a burning account look idle and slow
its own polling. The store keeps the live windows beside the polled ones and
merges them at read time.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from ccshift import oauth
from ccshift.poll_policy import parse_reset_ts

WINDOW_KEYS = ("five_hour", "seven_day")

# Two reset instants this close are the same window. The endpoint reports
# sub-second ISO stamps (``…:59.989981``) and the statusLine whole epoch
# seconds, so exact equality never holds; a window boundary is minutes apart
# from any other account's.
RESET_TOL_S = 120.0
WEEK_S = 7 * 86400.0

# A reading older than this carries no information about the burn rate "now".
BURN_MAX_AGE_S = 1800.0
# Samples closer together than this give a rate dominated by quantization.
BURN_MIN_DT_S = 20.0
# Only extrapolate once a reading is older than this: inside a normal poll
# interval the reading is as fresh as the planner intends.
PROJECT_AFTER_S = 90.0
# Never extrapolate further than this. A rate measured during a burst says
# nothing about the next hour, and an unbounded projection would switch away
# from an account that went idle.
PROJECTION_HORIZON_S = 900.0


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def parse_statusline(payload: object) -> dict[str, dict] | None:
    """``{"five_hour": {"pct", "resets_ts"}, "seven_day": {...}}`` or None.

    Tolerant by construction: the payload comes from another program and each
    window may be independently absent (Claude Code drops a window once its
    ``resets_at`` passes). A window without a usable percentage is skipped;
    no usable window at all returns None.
    """
    if not isinstance(payload, dict):
        return None
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    out: dict[str, dict] = {}
    for key in WINDOW_KEYS:
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        pct = _finite(window.get("used_percentage"))
        if pct is None:
            continue
        resets = _finite(window.get("resets_at"))
        out[key] = {
            "pct": min(100.0, max(0.0, pct)),
            "resets_ts": resets if resets is not None and resets > 0 else None,
        }
    return out or None


def _reset_candidates(
    polled: dict | None, live: dict | None, key: str
) -> list[float]:
    """Every reset instant this account is known to have for one window."""
    found: list[float] = []
    if isinstance(polled, dict):
        window = polled.get(key)
        if isinstance(window, dict):
            ts = parse_reset_ts(window.get("resets_at"))
            if ts is not None:
                found.append(ts)
    if isinstance(live, dict):
        window = live.get(key)
        if isinstance(window, dict):
            ts = _finite(window.get("resets_ts"))
            if ts is not None:
                found.append(ts)
    return found


def _same_instant(a: float, b: float) -> bool:
    return abs(a - b) <= RESET_TOL_S


def _weeks_apart(later: float, earlier: float) -> bool:
    """Whether ``later`` is a whole number (≥1) of weeks after ``earlier``."""
    gap = later - earlier
    if gap < WEEK_S - RESET_TOL_S:
        return False
    remainder = gap % WEEK_S
    return remainder <= RESET_TOL_S or WEEK_S - remainder <= RESET_TOL_S


def match_account(
    reading: dict[str, dict],
    polled: dict[str, dict | None],
    live: dict[str, dict | None],
    now: float,
) -> str | None:
    """The one managed account this reading belongs to, or None.

    ``polled`` and ``live`` map slot → stored usage dict / stored live windows.
    Matching order: the weekly reset instant, then the 5h one, each requiring
    exactly one account to agree. A weekly window that has rolled since the
    account was last polled (its stored reset is in the past) matches a
    reading whose reset is a whole number of weeks later. Anything ambiguous
    or unmatched returns None — an unattributable reading is dropped rather
    than credited to the wrong account, because a wrong credit is a wrong
    switch decision.
    """
    for key in ("seven_day", "five_hour"):
        window = reading.get(key)
        if window is None or window.get("resets_ts") is None:
            continue
        target = window["resets_ts"]
        exact = [
            num
            for num in polled.keys() | live.keys()
            if any(
                _same_instant(target, ts)
                for ts in _reset_candidates(polled.get(num), live.get(num), key)
            )
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None
        if key != "seven_day":
            continue
        rolled = [
            num
            for num in polled.keys() | live.keys()
            if any(
                ts <= now and _weeks_apart(target, ts)
                for ts in _reset_candidates(polled.get(num), live.get(num), key)
            )
        ]
        if len(rolled) == 1:
            return rolled[0]
        if len(rolled) > 1:
            return None
    return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _window_dict(pct: float, resets_iso: str | None) -> dict:
    window: dict = {"pct": pct}
    if resets_iso:
        window["resets_at"] = resets_iso
        try:
            window["countdown"], window["clock"] = oauth.format_reset(resets_iso)
        except ValueError:
            pass
    return window


def accept_window(
    stored: dict | None, polled_window: dict | None, incoming: dict
) -> dict | None:
    """The live window to store, or None when ``incoming`` must be ignored.

    Usage only rises inside a window, so within the same window a lower
    percentage is a stale copy (an idle session re-sending its last response)
    and is dropped; a later window replaces an earlier one; an earlier window
    is stale outright. ``stored`` is the live window already on file and
    ``polled_window`` the polled one — the reference is whichever is higher
    for the same window, so a live reading never undercuts a fresher poll.
    """
    pct = incoming["pct"]
    resets = incoming.get("resets_ts")
    references: list[tuple[float | None, float]] = []
    if isinstance(stored, dict) and _finite(stored.get("pct")) is not None:
        references.append((_finite(stored.get("resets_ts")), float(stored["pct"])))
    if isinstance(polled_window, dict) and _finite(polled_window.get("pct")) is not None:
        references.append(
            (parse_reset_ts(polled_window.get("resets_at")), float(polled_window["pct"]))
        )
    for ref_resets, ref_pct in references:
        if resets is None or ref_resets is None:
            if pct < ref_pct:
                return None
        elif _same_instant(resets, ref_resets):
            if pct < ref_pct:
                return None
        elif resets < ref_resets:
            return None
    return {"pct": pct, "resets_ts": resets}


def live_applies(live: dict | None, polled_at: float | None) -> bool:
    """Whether a stored live reading can still describe the account.

    A poll is the endpoint's own answer at the moment it was taken, so a live
    reading from BEFORE the last poll is superseded by it — outright, not
    window by window. "Usage only rises inside a window" holds for what a
    session keeps consuming, but it does not hold when the provider resets or
    re-grants quota early: the poll then reads lower, and a live reading that
    outlived it would keep showing (and the engine keep acting on) the old,
    higher number for as long as the window lasts.
    """
    if not isinstance(live, dict):
        return False
    if polled_at is None:
        return True
    changed = _finite(live.get("changedAt"))
    return changed is not None and changed > polled_at


def merge_live(
    polled: dict | None,
    live: dict | None,
    polled_at: float | None = None,
    now: float | None = None,
) -> tuple[dict | None, bool]:
    """``(usage, used_live)``: ``polled`` with live windows laid over it.

    Only a live reading taken after the last poll (``polled_at``) is laid over
    it (:func:`live_applies`), and only windows that have not reset since
    (``now``). Of those, a live window wins when it is the same window with a
    higher percentage, or a later window; otherwise the polled one stands.
    Scoped (per-model) windows and spend are never touched — the statusLine
    does not carry them. With nothing polled yet, the live windows stand alone.
    """
    if not live_applies(live, polled_at):
        return polled, False
    base = dict(polled) if isinstance(polled, dict) else {}
    used = False
    for key in WINDOW_KEYS:
        lw = live.get(key)
        pct = _finite(lw.get("pct")) if isinstance(lw, dict) else None
        if pct is None:
            continue
        resets = _finite(lw.get("resets_ts"))
        if now is not None and resets is not None and resets <= now:
            continue  # that window has rolled over: its number is obsolete
        pw = base.get(key) if isinstance(base.get(key), dict) else None
        if pw is None or _finite(pw.get("pct")) is None:
            base[key] = _window_dict(pct, _iso(resets) if resets else None)
            used = True
            continue
        p_resets = parse_reset_ts(pw.get("resets_at"))
        if resets is not None and p_resets is not None:
            if _same_instant(resets, p_resets):
                if pct > float(pw["pct"]):
                    base[key] = _window_dict(pct, pw.get("resets_at"))
                    used = True
            elif resets > p_resets:
                base[key] = _window_dict(pct, _iso(resets))
                used = True
        elif pct > float(pw["pct"]):
            # No reset on one side (an idle window the endpoint reports with
            # none): the higher percentage is the more recent fact.
            base[key] = _window_dict(
                pct, _iso(resets) if resets is not None else pw.get("resets_at")
            )
            used = True
    return (base if used else polled), used


def view_signature(usage: dict | None) -> tuple:
    """What a reader of ``usage`` would act on: (pct, reset) per window.

    Countdown/clock strings are derived from the wall clock and differ between
    two otherwise identical merges, so "did the reading change" must not
    compare them.
    """
    sig: list[tuple] = []
    if isinstance(usage, dict):
        for key in WINDOW_KEYS:
            w = usage.get(key)
            if isinstance(w, dict):
                sig.append((key, w.get("pct"), w.get("resets_at")))
    return tuple(sig)


def update_burn(
    burn: dict | None,
    prev: dict | None,
    prev_at: float | None,
    new: dict | None,
    now: float,
) -> dict | None:
    """Fold one more consecutive pair of readings into the stored burn rates.

    ``burn`` maps window key → ``{"rate": %/s, "at": ts}``. A pair that
    straddles a window rollover, or sits closer than ``BURN_MIN_DT_S``,
    teaches nothing and leaves the previous rate alone. A flat pair records a
    rate of zero, which is evidence too: it stops an old burst's rate from
    projecting an account that has gone quiet.
    """
    if not isinstance(prev, dict) or not isinstance(new, dict) or prev_at is None:
        return burn
    dt = now - prev_at
    if dt < BURN_MIN_DT_S:
        return burn
    out = dict(burn) if isinstance(burn, dict) else {}
    changed = False
    for key in WINDOW_KEYS:
        pw, nw = prev.get(key), new.get(key)
        if not isinstance(pw, dict) or not isinstance(nw, dict):
            continue
        p_pct, n_pct = _finite(pw.get("pct")), _finite(nw.get("pct"))
        if p_pct is None or n_pct is None:
            continue
        p_reset = parse_reset_ts(pw.get("resets_at"))
        n_reset = parse_reset_ts(nw.get("resets_at"))
        if p_reset is not None and n_reset is not None and not _same_instant(
            p_reset, n_reset
        ):
            continue  # different windows
        if n_pct < p_pct:
            continue  # rolled without a reset stamp; nothing to learn
        out[key] = {"rate": (n_pct - p_pct) / dt, "at": now}
        changed = True
    return out if changed else burn


def project_usage(
    usage: dict | None, burn: dict | None, age_s: float | None, now: float
) -> dict | None:
    """``usage`` advanced by the learned burn rate over its age, or unchanged.

    Applies only past ``PROJECT_AFTER_S`` of age, only for a rate sampled
    within ``BURN_MAX_AGE_S`` of now, only to a window that has not reset
    since, and over at most ``PROJECTION_HORIZON_S``. Percentages cap at 100.
    """
    if (
        not isinstance(usage, dict)
        or not isinstance(burn, dict)
        or age_s is None
        or age_s <= PROJECT_AFTER_S
    ):
        return usage
    horizon = min(age_s, PROJECTION_HORIZON_S)
    out = dict(usage)
    moved = False
    for key in WINDOW_KEYS:
        window, sample = usage.get(key), burn.get(key)
        if not isinstance(window, dict) or not isinstance(sample, dict):
            continue
        pct, rate, at = (
            _finite(window.get("pct")),
            _finite(sample.get("rate")),
            _finite(sample.get("at")),
        )
        if pct is None or rate is None or at is None or rate <= 0:
            continue
        if now - at > BURN_MAX_AGE_S:
            continue
        reset = parse_reset_ts(window.get("resets_at"))
        if reset is not None and reset <= now:
            continue  # the window rolled; the old percentage is obsolete
        projected = min(100.0, pct + rate * horizon)
        if projected > pct:
            out[key] = {**window, "pct": projected}
            moved = True
    return out if moved else usage
