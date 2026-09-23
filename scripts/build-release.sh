#!/usr/bin/env bash
set -euo pipefail

script_path=${BASH_SOURCE[0]}
case "$script_path" in
  */*) script_dir=${script_path%/*} ;;
  *) script_dir=. ;;
esac
project_dir=$(cd -- "$script_dir/.." && pwd)
installer_template="$project_dir/scripts/install.sh"
dist_dir="$project_dir/dist"

if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'Building a release requires python3.' >&2
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'Building a release requires uv.' >&2
  printf '%s\n' 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
fi
if ! command -v mktemp >/dev/null 2>&1 || \
  ! command -v mv >/dev/null 2>&1 || \
  ! command -v rm >/dev/null 2>&1; then
  printf '%s\n' 'Building a release requires mktemp, mv, and rm.' >&2
  exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
  checksum_command=sha256sum
elif command -v shasum >/dev/null 2>&1; then
  checksum_command=shasum
else
  printf '%s\n' 'Building a release requires sha256sum or shasum.' >&2
  exit 1
fi

version=$(python3 - "$project_dir/pyproject.toml" "$project_dir/src/cc_swaper/__init__.py" "$installer_template" <<'PY'
import re
import sys
from pathlib import Path

pyproject_path, init_path, installer_path = map(Path, sys.argv[1:])
project_text = pyproject_path.read_text(encoding="utf-8")
in_project = False
project_version = None
for line in project_text.splitlines():
    stripped = line.strip()
    if stripped == "[project]":
        in_project = True
        continue
    if in_project and stripped.startswith("["):
        break
    if in_project:
        match = re.match(r"^\s*version\s*=\s*\"([^\"]+)\"\s*$", line)
        if match:
            project_version = match.group(1)
            break

init_text = init_path.read_text(encoding="utf-8")
init_match = re.search(r"^__version__\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]\s*$", init_text, re.MULTILINE)
if project_version is None or init_match is None:
    print("Could not read the project version from pyproject.toml and src/cc_swaper/__init__.py.", file=sys.stderr)
    raise SystemExit(1)
if project_version != init_match.group(1):
    print(
        f"Version mismatch: pyproject.toml says {project_version}, "
        f"but src/cc_swaper/__init__.py says {init_match.group(1)}.",
        file=sys.stderr,
    )
    raise SystemExit(1)
if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", project_version):
    print(f"Release version must use MAJOR.MINOR.PATCH format: {project_version}", file=sys.stderr)
    raise SystemExit(1)

installer_text = installer_path.read_text(encoding="utf-8")
release_markers = re.findall(r"^release_version=\x27([^\x27]+)\x27$", installer_text, re.MULTILINE)
if release_markers != ["__CC_SWAPER_RELEASE_VERSION__"]:
    print("scripts/install.sh must contain exactly one release version marker.", file=sys.stderr)
    raise SystemExit(1)
print(project_version)
PY
)

release_dir="$dist_dir/release-v$version"
if [[ -e "$release_dir" || -L "$release_dir" ]]; then
  printf 'Release output already exists; inspect or move it before rebuilding: %s\n' "$release_dir" >&2
  exit 1
fi
if [[ -L "$dist_dir" ]]; then
  printf 'Release output directory must not be a symlink: %s\n' "$dist_dir" >&2
  exit 1
fi

mkdir -p -- "$dist_dir"
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/cc-swaper-release-build-v${version}.XXXXXX")
stage_dir=
cleanup_stage() {
  if [[ -n "${stage_dir:-}" && -d "$stage_dir" ]]; then
    rm -rf -- "$stage_dir"
  fi
  if [[ -n "${build_dir:-}" && -d "$build_dir" ]]; then
    rm -rf -- "$build_dir"
  fi
}
trap cleanup_stage EXIT
stage_dir=$(mktemp -d "$dist_dir/.release-v${version}.XXXXXX")

if ! uv build --sdist --wheel --out-dir "$build_dir" "$project_dir"; then
  printf '%s\n' 'uv build failed; see the build output above for details.' >&2
  exit 1
fi

wheel_name="cc_swaper-${version}-py3-none-any.whl"
sdist_name="cc_swaper-${version}.tar.gz"
if [[ ! -f "$build_dir/$wheel_name" || ! -f "$build_dir/$sdist_name" ]]; then
  printf 'The build did not produce the expected assets: %s and %s.\n' "$wheel_name" "$sdist_name" >&2
  exit 1
fi
mv -- "$build_dir/$wheel_name" "$stage_dir/$wheel_name"
mv -- "$build_dir/$sdist_name" "$stage_dir/$sdist_name"

python3 - "$installer_template" "$stage_dir/install.sh" "$version" <<'PY'
import os
import re
import sys
from pathlib import Path

source_path, destination_path, expected_version = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
template = source_path.read_text(encoding="utf-8")
marker = "__CC_SWAPER_RELEASE_VERSION__"
if template.count(marker) != 1:
    print("scripts/install.sh must contain exactly one release version marker.", file=sys.stderr)
    raise SystemExit(1)
rendered = template.replace(marker, expected_version)
match = re.search(r"^release_version=\x27([^\x27]+)\x27$", rendered, re.MULTILINE)
if match is None or match.group(1) != expected_version:
    print("Generated installer version does not match the package version.", file=sys.stderr)
    raise SystemExit(1)
destination_path.write_text(rendered, encoding="utf-8")
destination_path.chmod(0o755)
PY

cd -- "$stage_dir"
if [[ "$checksum_command" == sha256sum ]]; then
  sha256sum "$wheel_name" "$sdist_name" install.sh > SHA256SUMS
else
  shasum -a 256 "$wheel_name" "$sdist_name" install.sh > SHA256SUMS
fi

if [[ -e "$release_dir" || -L "$release_dir" ]]; then
  printf 'Release output appeared while building; preserving it: %s\n' "$release_dir" >&2
  exit 1
fi
mv -- "$stage_dir" "$release_dir"
stage_dir=

printf 'Release assets (v%s):\n' "$version"
while read -r digest asset; do
  printf '  %s  %s\n' "$digest" "$asset"
done < "$release_dir/SHA256SUMS"
printf '  %s\n' 'SHA256SUMS'
printf 'Output: %s\n' "$release_dir"
