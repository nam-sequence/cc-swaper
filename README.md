# cc-swaper (`ccs`)

A CLI for using multiple **accounts already signed in to Claude Code / claude.ai**. Choose an account manually, or let `ccs` detect usage-limit notices and continue the same conversation with the next account.

## Installation

Requires Bash, `uv`, Python 3.10+, Claude Code, and `tmux` on `PATH`. The persistent service mode uses a macOS LaunchAgent. Directly tested with Claude Code 2.1.280. Install `uv` first; neither installer path bootstraps it:

```bash
brew install uv                  # macOS with Homebrew
python3 -m pip install --user uv # a machine with pip where user-package installation is allowed
```

If you do not use Homebrew or pip, see the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/), then verify the installation:

```bash
uv --version
```

### Install from the v0.6.0 GitHub Release

Download the standalone installer asset, then run it with Bash:

```bash
curl -fL -o install.sh https://github.com/nam-sequence/cc-swaper/releases/download/v0.6.0/install.sh
# Or download with wget: wget -O install.sh https://github.com/nam-sequence/cc-swaper/releases/download/v0.6.0/install.sh
bash install.sh
```

The installer downloads the v0.6.0 wheel and `SHA256SUMS` from that release and verifies the wheel's SHA-256 before running `uv tool install`. It then initializes profiles if needed and runs `ccs setup`, which installs the Zsh integration and starts the background monitor. To install only the CLI, without initializing profiles or running setup, use:

```bash
bash install.sh --no-setup
```

When you are ready to configure it, run `ccs init` if no profile exists yet, then `ccs setup` to enable Zsh and the background monitor.

The standalone installer also requires `curl` or `wget`, `awk`, `mktemp`, `rm`, and either `sha256sum` or `shasum`.

### Install from a local checkout

From the repository root, run the checkout's installer. It installs the local source tree rather than downloading a release wheel:

```bash
bash scripts/install.sh
```

Use `bash scripts/install.sh --no-setup` to install only the CLI. If you install manually with `uv tool install .`, run `ccs init` once if no profile exists yet, then run `ccs setup` to enable Zsh and the LaunchAgent.

### Build and verify release assets

From the repository root, build the versioned release directory with:

```bash
bash scripts/build-release.sh
```

The script requires `sha256sum` or `shasum`. It checks that the version in `pyproject.toml`, `src/cc_swaper/__init__.py`, and the generated installer agree, then builds the wheel and source distribution with `uv build`. For v0.6.0 the assets are staged in `dist/release-v0.6.0`:

| Asset | Filename | Purpose |
| --- | --- | --- |
| Wheel | `cc_swaper-0.6.0-py3-none-any.whl` | Installable Python package |
| Source distribution | `cc_swaper-0.6.0.tar.gz` | Source package |
| Standalone installer | `install.sh` | Downloads and verifies the release wheel, then installs it |
| Checksums | `SHA256SUMS` | SHA-256 entries for the wheel, source distribution, and installer |

`SHA256SUMS` checks that downloaded files match the release manifest. It is not a separate publisher signature.

Verify the staged assets from that directory with either available checksum command:

```bash
cd dist/release-v0.6.0
sha256sum -c SHA256SUMS
# On macOS, use: shasum -a 256 -c SHA256SUMS
```

```bash
ccs list                         # the current Claude Code account is registered as "main"
ccs add secondary                # open the official Claude sign-in page for a second account
ccs list --show-identity         # check email/org to avoid signing in to the same account twice
```

To avoid opening a browser, run `ccs add secondary --no-login`, then `ccs login secondary`. `ccs` does not ask for a password or token. Each new profile gets its own Claude configuration directory; the default account uses the machine's existing configuration.

## Everyday use

```bash
cd /path/to/project
claude                           # start a NEW managed session for this repo and attach immediately
# Ctrl-b d                         detach; the Claude session keeps running
ccs run --detach -- --model sonnet # start a background session and return to the shell
ccs attach                       # attach to a session; choose one if this repo has several
ccs attach --session a1b2c3d4e5f6 # attach by run ID
ccs stop                         # stop a session; choose one if this repo has several
ccs stop --session a1b2c3d4e5f6   # stop by run ID
ccs status                       # view the service and the sessions it monitors
ccs usage                        # view the 5-hour / 7-day usage windows for all accounts
ccs usage main                   # view one profile only
ccs usage --json                 # structured data for scripts
ccs run --foreground             # run directly in this terminal
ccs use                          # open the account list and choose one for the next run
ccs use secondary                # choose a profile for the next run after exiting Claude
ccs resume                       # resume the last conversation; attach in an interactive terminal
claude -c                        # resume the last conversation, attaching if it is already active
ccs switch                       # choose an account and continue a conversation
ccs switch main                  # switch manually and continue the last conversation
ccs run --no-auto -- -p "Hi"     # non-interactive mode; do not switch automatically
```

`ccs run -- --model sonnet` passes the argument to the first Claude launch. When switching accounts automatically, `ccs` forks a new session from the transcript with `claude --resume <transcript-path> --fork-session`. The old transcript remains unchanged. It provides a cautious prompt asking Claude to check the state before continuing, and uses permission mode `manual` for the first continuation turn. It does not replay the original prompt verbatim, because that prompt may have modified files or called tools before usage ran out. Claude flags passed to the first launch are not passed again during an automatic switch; configure any options that need to persist in each profile.

After installation, open a new Zsh terminal and type `claude`: `ccs` creates a new tmux session for the current project and attaches to it. Run `ccs run --detach` when you want a session to start in the background without attaching; use `Ctrl-b d` to detach from an attached session while leaving it running. `claude -c` maps to `ccs resume`, which resumes the last conversation and attaches to it if that conversation is already active in a managed session with a known ID. For a still-running legacy session created before v0.6.0 whose conversation ID cannot be verified, use `ccs attach` to choose it directly. Multiple managed sessions can run for the same project. `ccs attach` and `ccs stop` act on the only session when there is one, or open a picker when there are several; pass `--session <run-id>` to select one explicitly, including in a non-interactive shell. The short run ID appears in the picker and `ccs status`. The LaunchAgent stays running to record the status of sessions managed by `ccs`; closing the terminal does not stop them. Administrative commands such as `claude auth status`, `claude --version`, and `-p` / `--bg` modes go through `ccs native` with the selected profile; these native modes do not switch accounts automatically. The service does not switch accounts for Claude sessions started directly outside the wrapper.

Each plain `claude` invocation starts a new managed session for the current project, so existing sessions can remain detached while you work in another. `ccs attach` and `ccs stop` let you return to or stop a chosen session.

`ccs usage` calls Claude Code's local `/usage` command separately for each profile; it does not create a transcript or a model turn. It checks up to eight accounts in parallel. In a terminal, the table shows each account's state (Loading, Queued, Done, or Error), then updates the 5-hour / 7-day percentage bars. The final table includes reset times and model-specific 7-day limits when available. When Claude does not provide a reset time (for example, when the 5-hour window is at 0%), the CLI reports **Reset time unavailable** instead of calculating one. When output is redirected or `ccs usage --json` is used, the CLI prints only the final result, without animation. See the [`/usage` documentation](https://code.claude.com/docs/en/commands).

`ccs switch` and `ccs use` without an account name open a numbered menu when run in a terminal; you can also enter an account name (prefix it with `@` if the name contains only digits). Choose `0` or press Enter to cancel. In a script or when input is redirected, pass the name directly, for example, `ccs switch secondary`. A switch continues the last conversation in a new managed session; other, unrelated conversations for the project can remain running. Stop the source conversation first if it is still active (`ccs stop --session <run-id>`). Use `ccs attach` or `ccs stop` to choose one of the project's sessions.

To remove a secondary account from the CLI:

```bash
ccs remove secondary              # log out, remove the profile from the list, and save its history in ~/.config/cc-swaper/removed
ccs remove secondary --purge-data # log out and also delete this profile's local Claude data
```

Exit any sessions using the profile before removing it. `main` (the machine's default Claude account) cannot be removed with `ccs remove`. The `--purge-data` option does not touch `~/.claude`. To uninstall the entire CLI, run `ccs service uninstall`, `ccs shell uninstall`, then `uv tool uninstall cc-swaper`.

`ccs` switches only when Claude Code's `StopFailure` hook reports `rate_limit` **and** includes a clear account-limit notice, such as `You've hit your limit · resets ...` or `This request would exceed your account's rate limit`. Similar text in a normal answer does not trigger a switch. A generic `429` error, network errors, and authentication errors do not switch accounts. If hooks are disabled or Anthropic changes the error format, automatic switching may not trigger; you can still exit the session and use `ccs switch <profile>`. The CLI rejects `--bare` and `--safe-mode` in auto mode, as well as modes that do not save transcripts. If all logged-in accounts are exhausted, the CLI stops and keeps the transcript so you can use `ccs resume` later. `ccs list` checks login status only; it does not read quota from the server.

## Limits of session continuation

Claude Code **must restart its process** when switching OAuth accounts. The CLI keeps the working directory and forks from the full conversation history; it cannot precisely continue a token generation in progress or an external command that is running. The CLI waits briefly for the transcript to stop being written before stopping the old process, but cannot guarantee that an in-progress operation has finished. The continuation prompt asks Claude to review the state to avoid repeating an action. Transferring a transcript between two `CLAUDE_CONFIG_DIR` directories by absolute path relies on Claude Code's `--resume <transcript path>` and `--fork-session` capabilities. On this machine, a small test with Claude Code 2.1.280 confirmed that a fork between two different accounts preserved context and left the source transcript unchanged. Switching after a **real usage limit** has not been verified; Anthropic has also not made a separate commitment about resuming between two accounts. If Claude refuses to open the transcript in the new profile, the old session file remains where it was.

`ccs` does not read, copy, or store credentials. On macOS, Claude Code manages credentials in Keychain; `ccs` only calls `claude auth login` and `claude auth status`. The `ccs` registry is at `~/.config/cc-swaper` (change it with `CC_SWAPER_HOME`) and contains only profile names, configuration paths, the current selection, IDs, and transcript paths. Email/org details are read from `auth status` only to display them on request and avoid automatically switching to the same account. `ANTHROPIC_*` environment variables that could override a claude.ai login are removed from the child process. Proxy/CA settings and Claude configuration in the project are still used; run the CLI in a shell and project you trust. When switching accounts, the full chat history and tool output in the old transcript are included in the new account's context.

Claude Code documentation: [multiple accounts with `CLAUDE_CONFIG_DIR`](https://code.claude.com/docs/en/env-vars), [where credentials are stored](https://code.claude.com/docs/en/authentication), [resume and fork](https://code.claude.com/docs/en/sessions), [the `StopFailure` hook](https://code.claude.com/docs/en/hooks).
