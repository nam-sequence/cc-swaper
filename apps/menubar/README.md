# CC Swaper Menu Bar

Native macOS 14+ menu bar app for viewing account usage and switching the account used by new Claude Code sessions.

## Build and test

```sh
swift test --package-path apps/menubar
bash apps/menubar/scripts/build-app.sh
```

The build script creates `apps/menubar/dist/CcsMenuBar.app`, sets `LSUIElement` so the app stays out of the Dock, and ad-hoc signs the local bundle. It replaces only a prior app with the same bundle identifier. `CC_SWAPER_APP_VERSION` can override the bundle version (strict `MAJOR.MINOR.PATCH`; defaults to the repository version), and `CC_SWAPER_APP_OUTPUT` can select an absolute staging path ending in `CcsMenuBar.app`.

## CLI connection

The app runs the installed absolute `ccs` launcher with an explicit argument vector and no shell. It preserves a stable launcher symlink, walks every resolved parent directory to `/` and checks ownership, POSIX write permissions, and ACL write/delete grants before every call, and uses a dedicated process group so timed-out usage or auto-switch checks stop their child processes. Legacy usage waits up to 55 seconds per batch of eight profiles plus a 15 second margin. Only the paths and configuration variables `ccs` needs are inherited; credential environment variables are omitted. The app searches `~/.local/bin/ccs`, Homebrew's standard bin directories, and absolute entries from `PATH`. Use **Choose CLI…** to select another installed `ccs` executable.

The menu keeps engine accounts and existing ccs profiles visible together under **Engine Accounts** and **Existing Profiles**. It reads routing from `ccs routing --json`; each Switch button calls the matching account engine or legacy profile command. Existing profiles remain available independently; no profile import runs automatically. Opening or refreshing the app never changes the selected account by itself.

**Launch at login** is off until enabled in Settings and uses macOS `SMAppService`. If macOS requires approval, the app offers a direct link to Login Items. The app never registers itself during startup.

**Automatic account switching** is also off by default and persisted only after the user turns it on. It runs `ccs accounts auto --once --json` serially every minute, with threshold and dry-run controls. It remains paused unless `ccs routing --json` reports `launchBackend: "accounts"`, because auto-switching engine accounts cannot affect new Claude sessions routed through the legacy profile wrapper. `ccs` remains the policy owner. Dry run is an explicit preview; it does not switch accounts.

The app requires version 1 JSON for profile and account list/status/switch responses. It accepts the existing `ccs usage --json` response for legacy usage. A future schema version or timed-out command is shown as an error. Credentials and Keychain data are not read by the Swift app; authentication and account selection remain with `ccs`.
