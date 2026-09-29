#!/bin/bash
# Remove the old cc-swaper (`ccs`) tool and every piece of its data so that
# ccshift can be set up from scratch. Also removes a leftover claude-swap
# (`cswap`) store. Claude Code's own default login and history (~/.claude,
# ~/.claude.json, Keychain item "Claude Code-credentials") are never touched.
#
# Dry run by default: it only prints what it would remove. Pass --apply to
# delete. Run it from a plain Terminal after quitting every Claude Code session
# that was started through ccs.

# Refuse other interpreters (sh runs bash in POSIX mode, zsh globs differently)
# before anything can change; the rest of this script needs real bash.
if [ -z "${BASH_VERSION:-}" ] || shopt -oq posix 2>/dev/null; then
  echo "error: run this script with bash: bash scripts/remove-cc-swaper.sh" >&2
  exit 1
fi
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1  # a dry run must not write anything, not even .pyc caches

usage() {
  cat <<'EOF'
Usage: scripts/remove-cc-swaper.sh [--apply] [--kill-running] [--keep-history-backup]

  (no flags)             Show what would be removed. Nothing is changed.
  --apply                Remove it.
  --kill-running         With --apply: stop every process that still uses the
                         old data first (Claude Code sessions started through
                         ccs, the ccs tool and its tmux server).
  --keep-history-backup  Move ~/.config/cc-swaper/session-backups to
                         ~/cc-swaper-session-backups instead of deleting it.
EOF
}

apply=0
kill_running=0
keep_history=0
for arg in "$@"; do
  case "$arg" in
    --apply) apply=1 ;;
    --kill-running) kill_running=1 ;;
    --keep-history-backup) keep_history=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
act() {
  # act "description" command...
  local description=$1; shift
  if (( apply )); then
    say "remove: $description"
    "$@"
  else
    say "would remove: $description"
  fi
}

[[ "$(uname -s)" == Darwin ]] || die "this script is for macOS"
[[ -n "${HOME:-}" && "$HOME" == /* && -d "$HOME" ]] || die "HOME is not a usable absolute directory"
while [[ "$HOME" == */ && "$HOME" != / ]]; do HOME=${HOME%/}; done
if [[ -n "${CLAUDECODE:-}" || -n "${CLAUDE_CODE_ENTRYPOINT:-}" ]]; then
  die "run this from a plain Terminal, not from inside Claude Code"
fi

old_root="$HOME/.config/cc-swaper"
upstream_root="$HOME/.claude-swap-backup"
upstream_xdg_root="$HOME/.local/share/claude-swap"
roots=("$old_root" "$upstream_root" "$upstream_xdg_root")
app="$HOME/Applications/CcsMenuBar.app"
bundle_id="com.namsequence.ccswaper.menubar"
zdotdir=${ZDOTDIR:-$HOME}
zshrc="${zdotdir%/}/.zshrc"
history_backup="$old_root/session-backups"
history_target="$HOME/cc-swaper-session-backups"

# Old release builds are removed only when this script runs from a ccshift checkout.
repo_dir=""
script_path=${BASH_SOURCE[0]:-}
if [[ -f "$script_path" ]]; then
  candidate=$(cd "$(dirname "$script_path")/.." && pwd -P)
  if /usr/bin/grep -q '^name = "ccshift"$' "$candidate/pyproject.toml" 2>/dev/null; then
    repo_dir=$candidate
  fi
fi

case "${CLAUDE_CONFIG_DIR:-}" in
  "$old_root"|"$old_root"/*) die "CLAUDE_CONFIG_DIR points into $old_root; open a new Terminal first" ;;
esac

# Every process that still uses the old data: the ccs tool, its tmux server,
# anything whose command line points into a root being removed, and Claude
# Code itself when its environment has CLAUDE_CONFIG_DIR=<old profile>.
# Other programs that merely inherited that variable (dev servers or MCP
# servers started from a ccs session) do not use the data and are left alone.
# This script and its parent shells are never listed.
holders() {
  python3 - "${roots[@]}" <<'PY'
import os, subprocess, sys
roots = sys.argv[1:]

def table(*flags):
    result = subprocess.run(["ps", *flags, "-axww", "-o", "pid=,ppid=,stat=,command="],
                            capture_output=True, text=True)
    rows = {}
    for row in result.stdout.splitlines():
        parts = row.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = (int(parts[1]), parts[3], parts[2])
    # A healthy listing always contains this process; fail closed otherwise.
    if result.returncode != 0 or os.getpid() not in rows:
        sys.exit(3)
    return rows

argv = table()          # command lines only
full = table("-E")      # command lines followed by the environment
skip, pid = set(), os.getpid()
while pid and pid not in skip:
    skip.add(pid)
    pid = argv.get(pid, (0, ""))[0]
argv_needles = ["/uv/tools/cc-swaper/", "tmux -L cc-swaper"] + [root + "/" for root in roots]
env_needles = [f"CLAUDE_CONFIG_DIR={root}/" for root in roots] + [f"CLAUDE_CONFIG_DIR={root} " for root in roots]
for pid in sorted(argv):
    command, state = argv[pid][1], argv[pid][2]
    # Zombies and processes already exiting hold nothing and cannot be signalled.
    if pid in skip or command.startswith("ps ") or "Z" in state or "E" in state:
        continue
    executable = command.split(" ", 1)[0]
    is_claude = "/claude/versions/" in executable or os.path.basename(executable) == "claude"
    environment = full.get(pid, (0, ""))[1] + " "
    if any(n in command + " " for n in argv_needles) or (is_claude and any(n in environment for n in env_needles)):
        print(f"{pid}\t{command[:140]}")
PY
}
# Sets $holder_list; dies (in this shell, not a subshell) if ps cannot be read.
refresh_holders() {
  holder_list=$(holders) || die "could not list running processes; refusing to continue"
}

# zshrc block state: present, absent or malformed (start marker without end).
zshrc_block() {
  python3 - "$1" "$zshrc" <<'PY'
import os, sys, tempfile
mode, path = sys.argv[1], sys.argv[2]
start_marker, end_marker = "# >>> cc-swaper shell >>>", "# <<< cc-swaper shell <<<"
if not os.path.isfile(path):
    print("absent"); sys.exit(0)
real = os.path.realpath(path)  # edit the real file if .zshrc is a symlink
with open(real, encoding="utf-8", newline="") as handle:
    lines = handle.readlines()
starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == start_marker]
if not starts:
    print("absent"); sys.exit(0)
ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == end_marker]
pairs = []
for start in starts:
    end = next((i for i in ends if i > start), None)
    if end is None or any(start < other < end for other in starts):
        print("malformed"); sys.exit(0)
    pairs.append((start, end))
if mode == "check":
    print("present"); sys.exit(0)
for start, end in reversed(pairs):
    del lines[start:end + 1]
info = os.stat(real)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".zshrc.ccshift-")
with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
    handle.writelines(lines)
os.chmod(tmp, info.st_mode & 0o7777)
os.replace(tmp, real)
print("removed")
PY
}

# ---------------------------------------------------------------------------
# Preflight. Nothing is changed until every check here has passed.

real_projects=$(python3 -c 'import os; print(os.path.realpath(os.path.expanduser("~/.claude/projects")))')
for root in "${roots[@]}"; do
  [[ ! -L "$root" ]] || die "$root is a symlink; remove it and its target by hand, then rerun"
  real_root=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$root")
  case "$real_projects/" in
    "$real_root"/*) die "$HOME/.claude/projects resolves into $root; move it out before running this" ;;
  esac
done

zsh_state=$(zshrc_block check)
[[ "$zsh_state" != malformed ]] || die "$zshrc has a cc-swaper start marker without a matching end marker; remove that block by hand, then rerun"

if (( keep_history )) && [[ -d "$history_backup" && ! -L "$history_backup" ]]; then
  [[ ! -e "$history_target" ]] || die "$history_target already exists; move it away first"
fi

if (( apply )); then say "Removing cc-swaper data."; else say "Dry run. Nothing is changed; pass --apply to remove."; fi
say ""

# Session history that exists only inside the roots being removed.
for root in "$old_root" "$upstream_root"; do
  [[ -d "$root" ]] || continue
  while IFS= read -r -d '' dir; do
    counts=$(python3 - "$dir" <<'PY'
import os, sys
src, live = sys.argv[1], os.path.expanduser("~/.claude/projects")
total = unique = 0
for dp, _dirs, names in os.walk(src):
    for name in names:
        if name.endswith(".jsonl"):
            total += 1
            rel = os.path.relpath(os.path.join(dp, name), src)
            unique += not os.path.exists(os.path.join(live, rel))
print(total, unique)
PY
)
    read -r total unique <<< "$counts"
    say "warning: $dir holds $total session transcript(s); $unique of them are not in ~/.claude/projects"
    if (( unique )); then
      if (( keep_history )) && [[ "$dir" == "$history_backup"/* ]]; then
        say "         They are kept: session-backups moves to $history_target."
      else
        say "         --apply deletes them. Add --keep-history-backup to keep session-backups, or copy them out first."
      fi
    fi
  done < <(/usr/bin/find "$root" -type d -name projects -not -path '*/projects/*' -print0 2>/dev/null)
done


refresh_holders
running=$holder_list
count=0
if [[ -n "$running" ]]; then
  count=$(printf '%s\n' "$running" | wc -l | tr -d ' ')
  say "Processes still using the old data ($count):"
  printf '%s\n' "$running" | head -n 25 | sed 's/^/  /'
  (( count <= 25 )) || say "  ... and $((count - 25)) more"
  if (( apply && ! kill_running )); then
    die "quit these first (end the Claude sessions started through ccs), or rerun with --apply --kill-running"
  fi
  say ""
fi

# ---------------------------------------------------------------------------
launch_agents=()
for plist in "$HOME"/Library/LaunchAgents/com.cc-swaper.*.plist \
             "$HOME"/Library/LaunchAgents/com.cswap.*.plist \
             "$HOME"/Library/LaunchAgents/com.claude-swap.*.plist; do
  if [[ -e "$plist" ]]; then launch_agents+=("$plist"); fi
done

# 1. Stop the processes that use the old data, and make sure they are gone.
if (( apply && kill_running )) && [[ -n "$running" ]]; then
  # A KeepAlive LaunchAgent would restart what is killed, so unload agents first.
  for plist in ${launch_agents[@]+"${launch_agents[@]}"}; do
    launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
  done
  pids=$(printf '%s\n' "$running" | cut -f1 | tr '\n' ' ')
  # shellcheck disable=SC2086
  kill -TERM $pids 2>/dev/null || true
  deadline=$((SECONDS + 10))  # give sessions time to shut down cleanly
  while (( SECONDS < deadline )); do
    refresh_holders
    [[ -z "$holder_list" ]] && break
    sleep 0.5
  done
  refresh_holders
  if [[ -n "$holder_list" ]]; then
    # shellcheck disable=SC2046
    kill -KILL $(printf '%s\n' "$holder_list" | cut -f1) 2>/dev/null || true
    sleep 1
    refresh_holders
  fi
  [[ -z "$holder_list" ]] || die "could not stop: $(printf '%s\n' "$holder_list" | cut -f1 | tr '\n' ' ')"
  say "stopped: $count process(es) that used the old data"
fi

# 2. The old menu bar app (0.x builds only; a new 1.x ccshift build is kept).
kept_new_app=0
if [[ -d "$app" ]]; then
  app_version=$(plutil -extract CFBundleShortVersionString raw "$app/Contents/Info.plist" 2>/dev/null || true)
  app_id=$(plutil -extract CFBundleIdentifier raw "$app/Contents/Info.plist" 2>/dev/null || true)
  if [[ "$app_id" == "$bundle_id" && "$app_version" == 0.* ]]; then
    if (( apply )); then
      osascript -e "quit app id \"$bundle_id\"" >/dev/null 2>&1 || true
      pkill -x CcsMenuBar 2>/dev/null || true
    fi
    act "$app (version $app_version)" rm -rf "$app"
  else
    kept_new_app=1
    say "keep: $app (version ${app_version:-unknown}, not an old cc-swaper build)"
  fi
fi
if (( kept_new_app )); then
  say "keep: menu bar app preferences ($bundle_id, used by the kept app)"
elif defaults read "$bundle_id" >/dev/null 2>&1; then
  act "menu bar app preferences ($bundle_id)" defaults delete "$bundle_id"
fi

# 3. The zsh `claude` wrapper block in ~/.zshrc (backed up first).
if [[ "$zsh_state" == present ]]; then
  remove_block() {
    local backup
    backup="$zshrc.pre-ccshift.$(date +%Y%m%d%H%M%S)"
    cp -p "$zshrc" "$backup"
    zshrc_block remove >/dev/null
    say "  backup: $backup"
  }
  act "cc-swaper block in $zshrc (the claude() wrapper)" remove_block
fi

# 4. LaunchAgents left by cc-swaper or claude-swap.
for plist in ${launch_agents[@]+"${launch_agents[@]}"}; do
  unload_and_remove() {
    launchctl bootout "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
    rm -f "$plist"
  }
  act "LaunchAgent $plist" unload_and_remove
done

# 5. Keychain items. Claude Code names a config dir's login
#    "Claude Code-credentials-<first 8 hex of sha256(CLAUDE_CONFIG_DIR)>".
services=$(python3 - "$old_root" "$old_root/account-engine" "$upstream_root" "$upstream_xdg_root" <<'PY'
import hashlib, json, os, re, sys, unicodedata
root, stores = sys.argv[1], sys.argv[2:]
dirs = set()
for sub in ("profiles", "removed"):
    base = os.path.join(root, sub)
    if os.path.isdir(base):
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if os.path.isdir(path) and not os.path.islink(path):
                dirs.add(path)
                archived = re.fullmatch(r"(.+)-\d{8}T\d{6}Z-[0-9a-f]+", name)
                if sub == "removed" and archived:
                    # An archived profile was signed in under its original path.
                    dirs.add(os.path.join(root, "profiles", archived.group(1)))
# Session-mode profiles (`ccs accounts run`, `cswap run`) use <store>/sessions/<n>-<slug>.
for store in stores:
    base = os.path.join(store, "sessions")
    if os.path.isdir(base):
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if os.path.isdir(path) and not os.path.islink(path):
                dirs.add(path)
if os.path.isdir(root):
    for name in os.listdir(root):
        if name.startswith("profiles.json"):
            try:
                with open(os.path.join(root, name), encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, ValueError):
                continue
            for profile in data.get("profiles", []) if isinstance(data, dict) else []:
                config_dir = profile.get("config_dir") if isinstance(profile, dict) else None
                if isinstance(config_dir, str) and config_dir.startswith(root + "/"):
                    dirs.add(config_dir)
for path in sorted(dirs):
    digest = hashlib.sha256(unicodedata.normalize("NFC", path).encode()).hexdigest()[:8]
    print(f"Claude Code-credentials-{digest}\t{path}")
print("cc-swaper-engine\tcc-swaper account engine")
print("claude-swap\tclaude-swap account backups")
PY
)
delete_service() {
  local tries=0
  while security delete-generic-password -s "$1" >/dev/null 2>&1; do
    tries=$((tries + 1))
    (( tries < 50 )) || break
  done
  ! security find-generic-password -s "$1" >/dev/null 2>&1
}
failed_services=()
while IFS=$'\t' read -r service owner; do
  [[ -n "$service" ]] || continue
  security find-generic-password -s "$service" >/dev/null 2>&1 || continue
  act "Keychain items \"$service\" ($owner)" delete_service "$service" || failed_services+=("$service")
done <<< "$services"
if (( ${#failed_services[@]} )); then
  die "could not delete Keychain item(s): ${failed_services[*]}. Unlock the login keychain or allow access when asked, then rerun. The data directories were kept so a rerun can find them."
fi

# 6. Installed tools.
if command -v uv >/dev/null 2>&1; then
  for tool in cc-swaper claude-swap; do
    if uv tool list 2>/dev/null | /usr/bin/grep -q "^$tool "; then
      act "uv tool $tool" uv tool uninstall "$tool"
    fi
  done
fi

# 7. Data directories and logs. Symlinks inside (profiles/*/projects point at
#    ~/.claude/projects) are unlinked first; their targets are not followed.
if (( keep_history )) && [[ -d "$history_backup" && ! -L "$history_backup" ]]; then
  if (( apply )); then
    mv "$history_backup" "$history_target"
    say "kept: $history_backup moved to $history_target"
  else
    say "would keep: $history_backup (moved to $history_target)"
  fi
fi
remove_tree() {
  /usr/bin/find "$1" -type l -delete
  rm -rf "$1"
  [[ ! -e "$1" ]] || die "$1 is still present; a process may still be writing to it"
}
for root in "${roots[@]}"; do
  if [[ -e "$root" ]]; then
    size=$(/usr/bin/du -sh "$root" 2>/dev/null | cut -f1 | tr -d ' ' || true)
    act "$root (${size:-?})" remove_tree "$root"
  fi
done
for log in "$HOME"/Library/Logs/com.cswap.menubar.* "$HOME"/Library/Logs/com.cc-swaper.*; do
  [[ -e "$log" ]] || continue
  act "$log" rm -f "$log"
done

# 8. Old cc-swaper release builds in this checkout.
if [[ -n "$repo_dir" ]]; then
  for artifact in "$repo_dir"/dist/cc_swaper-* "$repo_dir"/dist/release-v0.*; do
    [[ -e "$artifact" ]] || continue
    act "$artifact" rm -rf "$artifact"
  done
fi

say ""
say "Kept: ~/.claude, ~/.claude.json and the Keychain item \"Claude Code-credentials\" (your default Claude login and history)."
if (( apply )); then
  say "Done. Open a new Terminal so the old claude() wrapper is gone, then install ccshift."
fi
