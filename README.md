# cc-swaper (`ccs`)

A macOS CLI and native menu bar app for Claude Code accounts. The `ccs accounts`
engine adapts the [MIT-licensed claude-swap project](https://github.com/realiti4/claude-swap)
for account slots, quota-aware switching, usage, an optional auto-switcher,
parallel sessions, directory mappings, and a terminal dashboard. The Swift app
shows usage and lets you switch from the menu bar. Existing `ccs` profile
commands remain available separately.

## Install

Requires macOS 14+, Claude Code, Python 3.12+, and
[`uv`](https://docs.astral.sh/uv/getting-started/installation/). Building the
menu bar app requires Swift 6.2 and Xcode. The `claude` shell wrapper supports
interactive Zsh; `ccs` works from any shell. `tmux` is not required.

Install the current v0.9.0 source from this checkout. First preserve the
profile registry so a downgrade to v0.8.2 remains possible:

```bash
bash -euo pipefail -c '
  registry="${CC_SWAPER_HOME:-$HOME/.config/cc-swaper}/profiles.json"
  if [[ -f "$registry" && ! -e "$registry.pre-v0.9.0" ]]; then
    cp -p "$registry" "$registry.pre-v0.9.0"
  fi
  bash scripts/install.sh
'
ccs --version
```

The source installer installs the CLI, registers the existing Claude login as
`main` if needed, and runs `ccs setup`. Setup installs the Zsh wrapper and
removes the old background monitor. It leaves old tmux sessions running. Use
`--no-setup` to skip profile initialization and Zsh setup. The currently
published v0.8.2 release is the previous profile-only version; its installer
does not contain the account engine or Swift app.

To build the native app locally:

```bash
swift test --package-path apps/menubar
bash apps/menubar/scripts/build-app.sh
mkdir -p ~/Applications
ditto apps/menubar/dist/CcsMenuBar.app ~/Applications/CcsMenuBar.app
open ~/Applications/CcsMenuBar.app
```

The menu bar app resolves the installed `ccs` executable and offers **Choose
CLI…** if it is elsewhere. Its **Launch at Login** control uses macOS Login
Items; macOS may ask you to enable the app in System Settings. The app does
not start auto-switching until you enable that control separately.

If an independently installed `claude-swap` is present, verify the new CLI
and app first, stop its old menu-bar service if it is running, then run
`uv tool uninstall claude-swap`. This removes the old `cswap` executable; it
does not delete `~/.claude-swap-backup`. Do not run both automatic switchers
against Claude's default login.

## Account engine

`ccs accounts` has the upstream account-management command set in a dedicated
namespace. The new engine stores slot metadata under
`~/.config/cc-swaper/account-engine` and credential backups in its own
macOS Keychain service; it does not import the existing
`~/.config/cc-swaper/profiles` entries. Add each account again when ready.

```bash
ccs accounts add                         # save the current default Claude login
ccs accounts list                        # quota and reset-time dashboard
ccs accounts status                      # active default Claude account
ccs accounts switch 2                    # select an account for new `claude` launches
ccs accounts switch --strategy best      # choose by remaining quota
ccs accounts use                         # use the current default login, no switch
ccs accounts run 2 --share-history -- --resume  # parallel account, shared history
ccs accounts auto --once --dry-run       # inspect an auto-switch decision
ccs accounts auto --threshold 80         # foreground auto-switch loop
ccs accounts map 2 ~/work/project        # use account 2 in that directory
ccs accounts tui                         # interactive terminal dashboard
ccs routing                              # show which backend `claude` uses
```

`ccs accounts --help` lists add-token, alias, move/swap, disable/enable,
unclaimed, configuration, export/import, usage import, purge, and the other
advanced commands. `add-token` accepts a hidden prompt or stdin. Never type a
token as a command argument: the CLI rejects it, but shell history or the
process list may already have recorded it. Exports are plaintext credentials;
store them privately.

`ccs accounts import backup.cswap` restores account login data and keeps only
`oauthAccount` from imported Claude configuration. To restore all settings
from a backup you trust, use `ccs accounts import backup.cswap --trust-config`
in an interactive terminal and type `TRUST` at the prompt. Full configuration
may include commands in hooks, status lines, or MCP servers.

Session mode keeps an account's transcripts separate by default. Add
`--share-history` when you want `/resume` to see the same project history;
the default-login switch path already uses Claude's normal `~/.claude`
history. Do not resume the same transcript in two terminals simultaneously.

The old `ccs switch <profile>` command selects an existing profile and routes
new `claude` launches through its separate `CLAUDE_CONFIG_DIR`. An engine
`switch` routes new launches through Claude's default login. The two groups
stay separate in the menu bar and no running Claude process changes account
mid-session. The legacy `main` profile is the same default `~/.claude` login
that the engine changes, so it cannot hold an independent account. Auto-switch
stays off by default and pauses while an existing profile controls new
launches. Use `ccs accounts run` for simultaneous account sessions without
changing the global route.

If the separately installed upstream `claude-swap` is still present, keep
only one auto-switcher enabled: both can change Claude's default login and
their per-account token backups do not share a refresh lock. Installing this
package never imports or deletes `~/.claude-swap-backup`.

Existing profile registries are upgraded to version 2 when first changed by
this build. Older ccs v0.8.2 cannot read that registry afterwards; reinstall
the new build before using the old profile commands again. This upgrade does
not remove any profile, transcript, skill, plugin, or running Claude session.

To roll back the CLI before using new account-engine data, restore the saved
`profiles.json.pre-v0.9.0` (mode 0600) and reinstall the v0.8.2 wheel from
its [GitHub release](https://github.com/nam-sequence/cc-swaper/releases/tag/v0.8.2).
Keep the v0.9.0 engine store intact until you have confirmed any accounts you
added no longer need it. The previous `claude-swap` tool can be reinstalled
separately without importing or removing its saved backup store.

## Existing profile commands

```bash
ccs list                        # * marks the selected account
ccs add work                    # add a profile and sign in via Claude Code
ccs switch                      # use Up/Down, Enter to select, Esc to cancel
ccs switch work                 # select by name
claude                          # native Claude with the selected account
claude -c                       # continue a shared session using the selected account
ccs usage                       # table for all accounts, checked in parallel
ccs usage work                  # one account
ccs usage --json                # machine-readable report
```

`ccs switch` changes only the account used by **new** `claude` processes. It does not change an open Claude process or launch Claude. Exit Claude and run `claude` again after switching. The new process can find conversations in the shared project history with Claude's native `-c`, `-r`, or `/resume` commands. Do not resume the same conversation in two terminals at once: their messages can interleave in one transcript.

To create a profile without opening the browser, use `ccs add work --no-login` and later `ccs login work`. `ccs list --show-identity` shows the email and organization reported by Claude Code. The `main` profile uses your existing default Claude configuration; added profiles get separate private configuration directories for account settings and credentials. Their `projects` entries link to `~/.claude/projects`, so `/resume` can see the same project transcripts under any selected profile. `ccs init` registers the default account on a fresh installation.

`ccs usage` invokes Claude Code's local `/usage` command for each profile, checking up to eight in parallel. The table shows loading states, percentage bars, 5-hour and 7-day windows, model-specific weekly limits when available, and Claude's reset times. Missing reset times display as **Reset time unavailable**. It does not show a subscription end date. Redirected output and `--json` omit the animation.

To remove an added profile, run `ccs remove work` to log out and archive its profile data, or `ccs remove work --purge-data` to delete that profile's local data. Shared project transcripts are kept by both options. You can also run `ccs remove main` to log out and unregister the default account. This archives only its ccs registration directory; `~/.claude`, including shared settings, skills, plugins, and project history, stays in place. `--purge-data` is unavailable for the default account. Exit any Claude process using a profile, including agents or background processes it started, before removing it; ccs refuses removal while that profile is locked. If you remove the last profile, `ccs add <name>` or `ccs init` can register a new one. An upgrade will not silently recreate a deliberately removed default account.

A registry without a default profile requires ccs v0.8.2 or later. Older versions expect the default profile to be present, so reinstall this version before using ccs again if you downgrade by accident.

## Migrating from v0.6

Installing v0.7 updates the Zsh wrapper and removes the LaunchAgent monitor. Open a new terminal or run `source ~/.zshrc` in an existing Zsh shell to load the new wrapper. New `claude` invocations use the selected account directly.

Old tmux sessions remain running so their current work is not interrupted. They retain the behavior they started with, including possible automatic switching, until they exit. Use `ccs attach` and `ccs stop` for these existing sessions, or inspect them with `tmux -L cc-swaper list-sessions`. These commands are retained only for migration; new sessions are not created in tmux.

The profile registry and sign-ins in `~/.config/cc-swaper` (or `$CC_SWAPER_HOME`) survive the upgrade. `ccs` does not read or copy Claude OAuth credentials. Claude Code handles those credentials; `ccs` invokes `claude auth login` and `claude auth status` under each profile. The CLI removes inherited environment variables that could override a claude.ai login. Claude project settings still apply, so use the wrapper in projects whose configuration you trust.

From v0.7.2, `ccs` accepts only an exact managed `projects` link to the user's `~/.claude/projects` directory and creates that link for new managed profiles. Existing physical `projects` directories are preserved; `ccs` does not merge or replace them automatically. Do not delete those directories to enable sharing. Shared transcripts and project auto memory are readable from every profile and may be sent to a different account when you resume them. Claude's `project purge` and transcript retention can affect this shared history for every profile.

From v0.8.0, normal Claude launches under an added profile also load a private snapshot of the default account's selected user preferences, authored skills, agents, rules, and commands. The profile's own settings file and account-synced skills remain untouched; values already set in that profile take precedence over the shared snapshot. Account authentication, permissions, trust, and remote-control settings are excluded. Unknown or unsupported settings keys stay profile-local. The default account is the source of truth for shared customizations. The snapshot uses `--settings`, which Claude applies above project and local settings for that session; shared plugin enablement can therefore override a project-level plugin choice. Authentication commands and `ccs usage` do not load this snapshot. ccs rejects native `claude --bg` for every profile because it cannot safely retain the profile lock after Claude detaches.

From v0.8.1, ccs repairs a managed profile's missing Claude TUI onboarding marker before launching a session if Claude reports that profile as already signed in. Claude can otherwise show its login wizard despite valid credentials, particularly after `claude auth login` or an IDE sign-in. The one-time repair changes only `hasCompletedOnboarding` in that profile's private `.claude.json`; it leaves the account and project values intact and does not read or copy credential tokens. ccs waits for another ccs launch to finish the same repair. If a different session keeps that profile in use, exit it and retry. Open a new terminal after upgrading so the Zsh wrapper uses the current Claude binary.

Marketplace plugins are offered from the default account's read-only plugin seed while each profile keeps its own synced plugins and mutable plugin state; marketplace entries with credential-bearing URLs are omitted. Credential-free user MCP definitions from the default account are passed to normal sessions through a private `--mcp-config` snapshot. By default, ccs skips definitions with headers, helpers, inline credential patterns, or a nonempty `env` map; the reviewed `FIRECRAWL_API_URL` with a credential-free URL is the only allowed environment entry. Other MCP definitions must be configured separately for that account. MCP OAuth sign-ins remain per account. To authenticate a shared MCP server, open a normal Claude session under the selected profile and use `/mcp`; the native `claude mcp` command does not load the temporary shared server list. Generated snapshots live inside each private managed profile and can contain MCP connection details; `ccs` never prints their values. Main-account plugin code, command-valued settings such as `statusLine`, and shared MCP commands run as your OS user inside the selected profile's Claude process. `CLAUDE_CONFIG_DIR` separates Claude's stored account data; it does not restrict those commands from reading other files or inherited environment variables available to your OS user. Restart a running Claude process to pick up changes to the default account's settings or resources.

To uninstall later, quit and remove `CcsMenuBar.app`, run `ccs shell uninstall`,
then `uv tool uninstall cc-swaper`. This leaves the profile registry, account
engine backups, and Claude's own data intact. `ccs accounts purge` is a
separate, destructive action for the engine's own account data. Purge removes
the engine data root and its Keychain backups after active session/refresh
checks. Small private coordination files remain in `~/.config/cc-swaper` to
block late refreshes; an explicit new account add/import reopens the engine.

## Build a release

Run `bash scripts/build-release.sh` on macOS to build a versioned directory
under `dist/`:

| Asset | Purpose |
| --- | --- |
| `cc_swaper-X.Y.Z-py3-none-any.whl` | Installable CLI wheel |
| `cc_swaper-X.Y.Z.tar.gz` | Source distribution |
| `CcsMenuBar-vX.Y.Z-macos-local.zip` | Ad-hoc-signed local macOS menu bar app |
| `install.sh` | Version-pinned standalone installer |
| `SHA256SUMS` | SHA-256 hashes for those four assets |

Verify from the release directory with `shasum -a 256 -c SHA256SUMS`. The app
is ad-hoc signed for local use; public distribution may require a Developer ID
signature and notarization. Checksums establish consistency with the release
manifest; they are not a separate publisher signature.

Claude Code references: [configuration directories](https://code.claude.com/docs/en/env-vars), [authentication](https://code.claude.com/docs/en/authentication), [sessions](https://code.claude.com/docs/en/sessions), and [`/usage`](https://code.claude.com/docs/en/commands).
