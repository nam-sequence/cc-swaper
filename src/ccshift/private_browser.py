"""Open a URL in a private window (macOS).

``ccshift add --login --private`` points Claude Code's ``$BROWSER`` at a small
wrapper around this module, so the sign-in page opens in a private window.
A private window shares no cookies with the browser's normal windows, so a
browser already signed in to another Claude account does not authorize that
account instead of the one being added.

The menu bar app opens the page in its own private sign-in window instead
(``ASWebAuthenticationSession``, which works whatever the default browser is):
``--handoff-file PATH`` writes the URL to a file the app watches rather than
opening any browser.

Run as ``python -m ccshift.private_browser [--browser BUNDLE_ID] URL``. Without
``--browser`` the default browser is used when it can open a private window
from the command line; otherwise the first installed browser that can. Safari
and Arc cannot (Arc's AppleScript ignores the incognito mode of a new window),
so when no such browser is installed the URL opens in a normal window, with a
note on stderr.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

# Bundle identifier (lower case) → the arguments that open a private window.
# Chromium-based browsers forward the flag to a running instance; Firefox takes
# the URL as the value of -private-window. In order of preference for the
# fallback when the default browser has no private mode.
PRIVATE_ARGUMENTS: dict[str, list[str]] = {
    "com.google.chrome": ["--incognito"],
    "com.brave.browser": ["--incognito"],
    "com.microsoft.edgemac": ["--inprivate"],
    "org.mozilla.firefox": ["-private-window"],
    "com.vivaldi.vivaldi": ["--incognito"],
    "com.operasoftware.opera": ["--private"],
    "org.chromium.chromium": ["--incognito"],
    "app.zen-browser.zen": ["-private-window"],
    "com.google.chrome.beta": ["--incognito"],
    "com.google.chrome.dev": ["--incognito"],
    "com.google.chrome.canary": ["--incognito"],
    "com.brave.browser.beta": ["--incognito"],
    "com.brave.browser.nightly": ["--incognito"],
    "com.microsoft.edgemac.beta": ["--inprivate"],
    "com.microsoft.edgemac.dev": ["--inprivate"],
    "com.microsoft.edgemac.canary": ["--inprivate"],
    "org.mozilla.firefoxdeveloperedition": ["-private-window"],
    "org.mozilla.nightly": ["-private-window"],
}

_LAUNCH_SERVICES_PLIST = (
    Path.home()
    / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
)


def default_browser_bundle_id(plist_path: Path = _LAUNCH_SERVICES_PLIST) -> str | None:
    """The bundle identifier that handles https links, or None when unset (Safari)."""
    try:
        with plist_path.open("rb") as handle:
            handlers = plistlib.load(handle).get("LSHandlers", [])
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    for scheme in ("https", "http"):
        for handler in handlers:
            if isinstance(handler, dict) and handler.get("LSHandlerURLScheme") == scheme:
                bundle = handler.get("LSHandlerRoleAll")
                if isinstance(bundle, str) and bundle:
                    return bundle
    return None


def is_installed(bundle_id: str) -> bool:
    """Whether Spotlight knows an app with this bundle identifier."""
    try:
        result = subprocess.run(
            ["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'c"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.endswith(".app") for line in result.stdout.splitlines())


def choose_browser(requested: str | None, default: str | None) -> str | None:
    """The browser to open privately: the requested one, else the default when
    it supports private windows, else the first installed one that does."""
    if requested:
        return requested if requested.lower() in PRIVATE_ARGUMENTS else None
    if default and default.lower() in PRIVATE_ARGUMENTS:
        return default
    return next((bundle for bundle in PRIVATE_ARGUMENTS if is_installed(bundle)), None)


def private_open_command(url: str, bundle_id: str) -> list[str]:
    arguments = PRIVATE_ARGUMENTS[bundle_id.lower()]
    # -n passes the arguments even when the browser is already running.
    return ["open", "-n", "-b", bundle_id, "--args", *arguments, url]


def hand_off(url: str, path: Path) -> None:
    """Write the URL for the menu bar app: private (0600) and all at once."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(url)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "--handoff-file":
        hand_off(args[2], Path(args[1]))
        return 0
    requested = None
    if len(args) == 3 and args[0] == "--browser":
        requested, args = args[1], args[2:]
    if len(args) != 1:
        print(
            "usage: python -m ccshift.private_browser [--browser BUNDLE_ID | --handoff-file PATH] URL",
            file=sys.stderr,
        )
        return 2
    url = args[0]
    bundle_id = choose_browser(requested, default_browser_bundle_id())
    if bundle_id is not None:
        if subprocess.run(private_open_command(url, bundle_id)).returncode == 0:
            return 0
    print(
        "No installed browser can open a private window from another app "
        "(Safari and Arc cannot); opening a normal window instead.",
        file=sys.stderr,
    )
    return subprocess.run(["open", url]).returncode


if __name__ == "__main__":
    sys.exit(main())
