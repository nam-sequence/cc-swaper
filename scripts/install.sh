#!/usr/bin/env bash
set -euo pipefail

# build-release.sh replaces this marker in the standalone release asset.
release_version='__CC_SWAPER_RELEASE_VERSION__'
release_repository='nam-sequence/cc-swaper'
wheel_prefix='cc_swaper-'

no_setup=0
while (($#)); do
  case "$1" in
    --no-setup)
      no_setup=1
      ;;
    -h|--help)
      printf '%s\n' 'Usage: install.sh [--no-setup]' \
        'Install ccs with uv. By default, initialize and configure ccs after installation.' \
        'Use --no-setup to install the CLI without running ccs init or ccs setup.'
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      printf '%s\n' 'Usage: install.sh [--no-setup]' >&2
      exit 2
      ;;
  esac
  shift
done

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'cc-swaper requires uv, but the uv command was not found on PATH.' >&2
  printf '%s\n' 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
fi

script_path=${BASH_SOURCE[0]}
case "$script_path" in
  */*) script_dir=${script_path%/*} ;;
  *) script_dir=. ;;
esac
project_dir=
if [[ "${script_dir##*/}" == scripts && \
  -f "$script_dir/../pyproject.toml" && \
  -f "$script_dir/../src/cc_swaper/__init__.py" ]]; then
  project_dir=$(cd -- "$script_dir/.." && pwd)
fi

if [[ -n "$project_dir" ]]; then
  install_target=$project_dir
else
  if [[ ! "$release_version" =~ ^[0-9]+(\.[0-9]+){2}$ ]]; then
    printf '%s\n' 'This standalone installer was not built as a versioned release asset.' >&2
    exit 1
  fi

  if command -v curl >/dev/null 2>&1; then
    download_command=curl
  elif command -v wget >/dev/null 2>&1; then
    download_command=wget
  else
    printf '%s\n' 'A standalone release install requires curl or wget to download its verified package.' >&2
    exit 1
  fi

  if command -v sha256sum >/dev/null 2>&1; then
    checksum_command=sha256sum
  elif command -v shasum >/dev/null 2>&1; then
    checksum_command=shasum
  else
    printf '%s\n' 'A standalone release install requires sha256sum or shasum to verify its package.' >&2
    exit 1
  fi

  if ! command -v mktemp >/dev/null 2>&1 || ! command -v rm >/dev/null 2>&1; then
    printf '%s\n' 'A standalone release install requires mktemp and rm to manage temporary downloads.' >&2
    exit 1
  fi
  if ! command -v awk >/dev/null 2>&1; then
    printf '%s\n' 'A standalone release install requires awk to read SHA256SUMS.' >&2
    exit 1
  fi

  wheel_name="${wheel_prefix}${release_version}-py3-none-any.whl"
  release_base="https://github.com/${release_repository}/releases/download/v${release_version}"
  download_dir=$(mktemp -d "${TMPDIR:-/tmp}/cc-swaper-install.XXXXXX")
  trap 'rm -rf -- "$download_dir"' EXIT
  wheel_path="$download_dir/$wheel_name"
  checksums_path="$download_dir/SHA256SUMS"

  download_asset() {
    local asset_name=$1
    local destination=$2
    local url="${release_base}/${asset_name}"

    if [[ "$download_command" == curl ]]; then
      if ! curl --proto '=https' --proto-redir '=https' -fL --retry 3 --connect-timeout 15 -o "$destination" "$url"; then
        printf 'Could not download release asset: %s\n' "$asset_name" >&2
        return 1
      fi
    else
      if ! wget --quiet --output-document="$destination" "$url"; then
        printf 'Could not download release asset: %s\n' "$asset_name" >&2
        return 1
      fi
    fi
  }

  download_asset "$wheel_name" "$wheel_path"
  download_asset SHA256SUMS "$checksums_path"

  expected_hash=$(awk -v asset="$wheel_name" '
    $2 == asset || $2 == "*" asset { count++; digest = $1 }
    END {
      if (count != 1) exit 1
      print digest
    }
  ' "$checksums_path") || {
    printf 'SHA256SUMS must contain exactly one entry for %s.\n' "$wheel_name" >&2
    exit 1
  }
  if [[ ! "$expected_hash" =~ ^[0-9a-f]{64}$ ]]; then
    printf 'SHA256SUMS contains an invalid SHA-256 digest for %s.\n' "$wheel_name" >&2
    exit 1
  fi

  if [[ "$checksum_command" == sha256sum ]]; then
    actual_hash=$(sha256sum "$wheel_path")
  else
    actual_hash=$(shasum -a 256 "$wheel_path")
  fi
  actual_hash=${actual_hash%% *}
  if [[ "$actual_hash" != "$expected_hash" ]]; then
    printf 'SHA-256 verification failed for %s; the package was not installed.\n' "$wheel_name" >&2
    exit 1
  fi

  install_target=$wheel_path
fi

uv tool install --force "$install_target"

if ((no_setup)); then
  exit 0
fi

if ! command -v ccs >/dev/null 2>&1; then
  printf '%s\n' 'ccs is not on PATH after installation.' >&2
  exit 1
fi

if ! ccs list >/dev/null 2>&1; then
  ccs init
fi

ccs setup
