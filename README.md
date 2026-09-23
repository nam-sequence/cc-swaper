# cc-swaper (`ccs`)

A small CLI for Claude Code accounts. Add profiles, choose which account new Claude processes use, and check usage. Switching is manual. New Claude sessions run directly in the current terminal, without tmux, a background monitor, or automatic account switching.

## Install

Requires Claude Code, Python 3.10+, and [`uv`](https://docs.astral.sh/uv/getting-started/installation/). The `claude` shell wrapper supports interactive Zsh; `ccs` works from any shell. `tmux` is no longer required.

Install from the v0.7.1 release:

```bash
curl -fL -o install.sh https://github.com/nam-sequence/cc-swaper/releases/download/v0.7.1/install.sh
bash install.sh
```

The script downloads the matching wheel and `SHA256SUMS`, verifies the wheel's SHA-256, installs it with `uv tool install`, registers the existing Claude login as `main` if needed, and runs `ccs setup`. Setup installs the new Zsh wrapper and removes the old background monitor. It does not stop old tmux sessions. Use `bash install.sh --no-setup` to skip profile initialization and Zsh setup; an old macOS monitor is still removed before replacement.

For a local checkout, run `bash scripts/install.sh`. With a manual `uv tool install .`, run `ccs init` if no profiles exist, then `ccs setup`.

## Everyday use

```bash
ccs list                        # * marks the selected account
ccs add work                    # add a profile and sign in via Claude Code
ccs switch                      # use Up/Down, Enter to select, Esc to cancel
ccs switch work                 # select by name
claude                          # native Claude with the selected account
claude -c                       # continue within that account
ccs usage                       # table for all accounts, checked in parallel
ccs usage work                  # one account
ccs usage --json                # machine-readable report
```

`ccs switch` changes only the account used by **new** `claude` processes. It does not change an open Claude process, transfer a conversation between accounts, or launch Claude. Exit Claude and run `claude` again after switching. Native `claude -c` and `claude -r` work with the selected account's history.

To create a profile without opening the browser, use `ccs add work --no-login` and later `ccs login work`. `ccs list --show-identity` shows the email and organization reported by Claude Code. The `main` profile uses your existing default Claude configuration; added profiles get separate private configuration directories. `ccs init` registers the default account on a fresh installation.

`ccs usage` invokes Claude Code's local `/usage` command for each profile, checking up to eight in parallel. The table shows loading states, percentage bars, 5-hour and 7-day windows, model-specific weekly limits when available, and Claude's reset times. Missing reset times display as **Reset time unavailable**. It does not show a subscription end date. Redirected output and `--json` omit the animation.

To remove an added profile, run `ccs remove work` to log out and archive its local history, or `ccs remove work --purge-data` to delete its local data. The default profile cannot be removed this way; neither option deletes `~/.claude`. Exit any Claude process using a profile before removing it.

## Migrating from v0.6

Installing v0.7 updates the Zsh wrapper and removes the LaunchAgent monitor. Open a new terminal or run `source ~/.zshrc` in an existing Zsh shell to load the new wrapper. New `claude` invocations use the selected account directly.

Old tmux sessions remain running so their current work is not interrupted. They retain the behavior they started with, including possible automatic switching, until they exit. Use `ccs attach` and `ccs stop` for these existing sessions, or inspect them with `tmux -L cc-swaper list-sessions`. These commands are retained only for migration; new sessions are not created in tmux.

The profile registry and sign-ins in `~/.config/cc-swaper` (or `$CC_SWAPER_HOME`) survive the upgrade. `ccs` does not read or store passwords or tokens. Claude Code handles its own credentials; `ccs` invokes `claude auth login` and `claude auth status` under each profile. The CLI removes inherited environment variables that could override a claude.ai login. Claude project settings still apply, so use the wrapper in projects whose configuration you trust.

To uninstall later, run `ccs shell uninstall`, then `uv tool uninstall cc-swaper`. This leaves the profile registry and Claude's own data intact.

## Build a release

Run `bash scripts/build-release.sh` to build `dist/release-v0.7.1/`:

| Asset | Purpose |
| --- | --- |
| `cc_swaper-0.7.1-py3-none-any.whl` | Installable wheel |
| `cc_swaper-0.7.1.tar.gz` | Source distribution |
| `install.sh` | Version-pinned standalone installer |
| `SHA256SUMS` | SHA-256 hashes for the other three assets |

Verify from the release directory with `shasum -a 256 -c SHA256SUMS` on macOS or `sha256sum -c SHA256SUMS` on Linux. Checksums establish consistency with the release manifest; they are not a separate publisher signature.

Claude Code references: [configuration directories](https://code.claude.com/docs/en/env-vars), [authentication](https://code.claude.com/docs/en/authentication), [sessions](https://code.claude.com/docs/en/sessions), and [`/usage`](https://code.claude.com/docs/en/commands).
