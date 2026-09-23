#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'cc-swaper requires uv, but the uv command was not found on PATH.' >&2
  printf '%s\n' 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
fi

uv tool install --force "$project_dir"

if ! command -v ccs >/dev/null 2>&1; then
  printf '%s\n' 'ccs is not on PATH after installation.' >&2
  exit 1
fi

if ! ccs list >/dev/null 2>&1; then
  ccs init
fi

ccs setup
