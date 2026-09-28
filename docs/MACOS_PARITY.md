# macOS account engine and menu bar plan

This work adapts the MIT-licensed `realiti4/claude-swap` source at commit
`3a4e5c14873eb5b32f182d55c68da98ac8c0db45` inside the existing cc-swaper
repository. The upstream license and attribution are retained in
`THIRD_PARTY_NOTICES.md`.

## Boundaries

- `src/claude_swap/` owns account credentials, macOS Keychain access, Claude's
  credential locks, usage collection, switching decisions, automatic rotation,
  session mode, import/export, settings, and the terminal dashboard. Its data
  root and per-account Keychain service are private to cc-swaper; the separately
  installed upstream `cswap` executable and store are never overwritten or
  migrated automatically.
- `src/cc_swaper/` keeps existing profile commands available. The new
  `ccs accounts ...` namespace forwards to the vendored engine. Existing ccs
  profiles and their `CLAUDE_CONFIG_DIR` directories are not migrated or
  deleted by installing this package. A versioned `launch_backend` choice in
  `profiles.json` decides whether new Zsh `claude` launches use the selected
  existing profile or Claude's default login managed by the engine.
- `apps/menubar/` is a SwiftUI status-item client. It invokes a fixed absolute
  `ccs` executable with the `accounts` namespace and argument arrays, then
  decodes schema-versioned JSON.
  It never reads Keychain items or decides which account has the most quota.

## Feature inventory

| Surface | Upstream behavior to retain | Verification |
| --- | --- | --- |
| Accounts | Add, list, status, direct/rotating switch, remove, disable/enable, aliases, move/swap slots, unclaimed entries, setup tokens and API keys | Upstream tests plus macOS isolated-store tests |
| Usage | 5-hour, 7-day, model limits, spend, reset times, stale/error states, pace, cached reads, JSON | Upstream collector/cache/JSON tests |
| Auto-switch | Thresholds, strategies, cooldown, hysteresis, model option, adaptive polling, quarantine, dry run, once, JSONL events | Upstream policy tests; default off |
| Sessions | Parallel account-specific Claude processes, optional shared history, shared customizations/MCP, directory mappings | Fake Claude integration tests; no silent legacy migration |
| Data | Config get/set/unset, export/import, usage import/hold, upgrade, purge | Isolated store tests and explicit confirmation for destructive actions |
| Terminal UI | Dashboard, watch view, auto view, keyboard selection and refresh | Upstream Textual pilot tests |
| Swift UI | Account/usage/status list, direct switch, refresh, errors/loading, auto settings, and launch at login | Swift fake-CLI tests and visible macOS QA |

The first integration slice makes the complete upstream CLI and TUI available
while preserving the old `ccs list` and `ccs switch` commands. `ccs switch`
routes new `claude` launches to the chosen existing profile; `ccs accounts
switch` routes them to Claude's default login after activating an account.
`ccs accounts use` chooses the current default login without switching it.
The Swift app starts with status, usage, refresh, and direct switch. Remaining
menu actions will use the same engine; no account operation will be reimplemented
in Swift.

## Existing profiles and safety

Legacy ccs profiles are separate Claude configuration directories; the new
engine stores account slots in Keychain. The user chose to keep the legacy
profiles separate and add accounts manually to the new engine. The installer
does not infer that a profile name identifies a particular Keychain credential,
copy credentials, or delete source profiles. The Swift app shows both groups
at the same time, with separate selection markers. A legacy `main` profile
and the engine's default login both use `~/.claude`, so they cannot represent
independent sign-ins. Existing Claude and tmux sessions remain untouched.

Automatic switching is off by default. The account engine may rotate the
default login only while `launch_backend=accounts`; it must report a paused
state when an existing profile is selected. The backend decision and live
activation need one serialized guard so a concurrent legacy selection cannot
leave a hidden default-login switch after the selection completes.

An upstream `claude-swap` v0.26.0 is already installed on this Mac. The vendored
engine must not install a `cswap` executable, read or migrate its
`~/.claude-swap-backup` store, or reuse its per-account Keychain service.
Both engines can affect Claude's default active credential, so only one
automatic switcher should be enabled when the new engine is put into use.

The upstream session mode can share settings, hooks, skills, and MCP definitions
across account contexts. That is a broader trust boundary than cc-swaper's
filtered snapshots, so the UI and documentation must identify it clearly.
Exported account files contain credentials and are plaintext; Swift must never
display or log them. Token entry uses a secure field and stdin, never argv.

Only macOS builds are supported. Account-changing live tests require a
disposable Keychain and fake Claude process before any real-account trial.
