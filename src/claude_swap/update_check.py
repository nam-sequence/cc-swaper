"""Check cc-swaper releases without upgrading the external upstream tool."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import NamedTuple

from claude_swap.cache import CACHE_DIR, MISSING, read_cache, write_cache

CACHE_PATH = CACHE_DIR / "update_check.json"
CACHE_TTL = 24 * 3600  # 24 hours
RELEASE_API_URL = "https://api.github.com/repos/nam-sequence/cc-swaper/releases/latest"
RELEASES_URL = "https://github.com/nam-sequence/cc-swaper/releases/latest"

_VERSION_RE = re.compile(
    r"(\d+(?:\.\d+)*)(?:[-_.]?(alpha|beta|preview|pre|rc|a|b|c)[-_.]?(\d+)?)?",
    re.IGNORECASE,
)
_PRE_RANKS = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "preview": 2, "pre": 2, "rc": 2, "c": 2}
_FINAL_RANK = 3


class _Version(NamedTuple):
    """A version as a sort key. NamedTuple so that comparing two of these
    compares the release first and only then the pre-release fields, which is
    what puts 0.27.0b1 below 0.27.0 and both below 0.28.0."""

    release: tuple[int, ...]
    pre_rank: int  # _FINAL_RANK when this is not a pre-release.
    pre_number: int


def _parse_version(v: str) -> _Version:
    """Parse a release number with an optional PEP 440 pre-release suffix.

    Every cycle of claude-swap ships as a pre-release first (0.27.0b1,
    0.26.0b1, ...), and a plain int() over the dotted parts raised ValueError
    on those, which check_for_update swallowed as "no update available".
    Development and post releases are not modeled — the project publishes
    none — so they collapse onto the release they belong to.
    """
    m = _VERSION_RE.match(v)
    if m is None:
        raise ValueError(f"unrecognized version: {v!r}")
    release = tuple(int(x) for x in m.group(1).split("."))
    # PEP 440 makes 0.27 and 0.27.0 the same release, and the pre-release rank
    # only means anything once the release segments line up.
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    if m.group(2) is None:
        return _Version(release, _FINAL_RANK, 0)
    return _Version(release, _PRE_RANKS[m.group(2).lower()], int(m.group(3) or 0))


def _is_newer(latest: str, current: str) -> bool:
    """Whether the latest release is an upgrade worth telling the user about."""
    latest_version = _parse_version(latest)
    current_version = _parse_version(current)
    # Nobody on a final release asked to be moved onto a pre-release, so we
    # stay quiet for them; people already running one still hear about later
    # pre-releases.
    if latest_version.pre_rank != _FINAL_RANK and current_version.pre_rank == _FINAL_RANK:
        return False
    return latest_version > current_version


def _detect_install_method() -> str | None:
    """Return 'uv', 'pipx', or None if we can't tell."""
    prefix = Path(sys.prefix)
    parts = tuple(p.lower() for p in prefix.parts)
    pairs = list(zip(parts, parts[1:]))

    if ("uv", "tools") in pairs:
        return "uv"
    if ("pipx", "venvs") in pairs:
        return "pipx"

    # Env-var override: only trust if sys.prefix is actually under it.
    for env_var, name in (("UV_TOOL_DIR", "uv"), ("PIPX_HOME", "pipx")):
        root = os.environ.get(env_var)
        if root:
            try:
                if prefix.is_relative_to(Path(root)):
                    return name
            except (ValueError, OSError):
                pass
    return None


def check_for_update(current_version: str) -> str | None:
    """Return an update notice for this cc-swaper distribution, if available."""
    try:
        latest_version = read_cache(CACHE_PATH, CACHE_TTL)
        if latest_version is MISSING:
            try:
                req = urllib.request.Request(
                    RELEASE_API_URL,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "cc-swaper",
                    },
                )
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read().decode())
                tag = data["tag_name"]
                latest_version = tag.removeprefix("v") if isinstance(tag, str) else None
            except Exception:
                latest_version = None
            write_cache(CACHE_PATH, latest_version)

        if latest_version and _is_newer(latest_version, current_version):
            return (
                f"A newer version of cc-swaper is available ({latest_version}). "
                f"You are using {current_version}. Update from {RELEASES_URL}."
            )
        return None
    except Exception:
        return None


def run_self_upgrade() -> int:
    """Refuse self-upgrade rather than replacing a separate upstream install."""
    from claude_swap.printer import error

    error(
        "This account engine is bundled with cc-swaper and will not upgrade the "
        "separate upstream claude-swap installation. Install the latest cc-swaper "
        f"release from {RELEASES_URL}."
    )
    return 1
