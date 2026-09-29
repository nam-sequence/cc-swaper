# ccshift Menu Bar

Native macOS 14+ menu bar app for viewing account usage and switching the Claude Code account managed by `ccshift`.

## Build and test

```sh
swift test --package-path apps/menubar
bash apps/menubar/scripts/build-app.sh
```

The build script creates `apps/menubar/dist/CcsMenuBar.app`, sets `LSUIElement` so the app stays out of the Dock, and ad-hoc signs the local bundle. It replaces only a prior app with the same bundle identifier. `CC_SWAPER_APP_VERSION` can override the bundle version (strict `MAJOR.MINOR.PATCH`; defaults to the version in the repository's `pyproject.toml`), and `CC_SWAPER_APP_OUTPUT` can select an absolute staging path ending in `CcsMenuBar.app`.

The app icon is an Icon Composer file (`Resources/AppIcon.icon`). The build script compiles it with Xcode's `actool` into `Assets.car`, which macOS 26 renders with Liquid Glass, plus an `AppIcon.icns` fallback for macOS 14 and 15. Without Xcode the app is built without an icon.

## CLI connection

The app runs the installed absolute `ccshift` launcher with an explicit argument vector and no shell. It preserves a stable launcher symlink, walks every resolved parent directory to `/` and checks ownership, POSIX write permissions, and ACL write/delete grants before every call, and uses a dedicated process group so timed-out checks stop their child processes. Only `HOME`, `USER`, `TMPDIR`, the locale variables and a fixed `PATH` are passed to `ccshift`; credential environment variables are omitted. The app searches `~/.local/bin/ccshift`, Homebrew's standard bin directories, and absolute entries from `PATH`. Use **Choose ccshift executable…** (or **Choose…** in the Settings window) to select another installed `ccshift`.

The menu lists the accounts from `ccshift list --json`. Each Switch button runs `ccshift switch <number> --json`; accounts held out of rotation with `ccshift disable` are marked *Not in auto-switch* and can still be switched to. Opening or refreshing the app never changes the active account by itself.

**Launch at login** is off until enabled in the Settings window (**ccshift Settings…** in the menu, or ⌘,) and uses macOS `SMAppService`. If macOS requires approval, the app offers a direct link to Login Items. The app never registers itself during startup.

**Automatic account switching** is also off by default and persisted only after the user turns it on. It runs `ccshift auto --once --json` serially every minute, with threshold and dry-run controls in the Settings window (the menu keeps a quick on/off toggle), and stays paused until at least one account exists (while the list is empty it is re-read every minute). `ccshift` remains the policy owner. Dry run is an explicit preview; it does not switch accounts. Run only one automatic switcher at a time (this app, `ccshift auto`, or `ccshift menubar`). Each check is a separate `ccshift auto --once` run, so the unhealthy-account failover of the long-running `ccshift auto` loop (switching away after several ticks with unreadable usage) does not apply here; switches at or near the limit do.

The app requires version 1 JSON for list and switch responses and for every auto-switch event. A future schema version, a usage error without JSON output, or a timed-out command is shown as an error. Credentials and Keychain data are not read by the Swift app; authentication and account selection remain with `ccshift`.
