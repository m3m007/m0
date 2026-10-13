#!/usr/bin/env bash
# Thin wrapper so this can be wired as a git pre-commit hook.
# Exits non-zero (blocking the commit) if any credential-shaped string
# is found; prints only FILE:LINE, never the value.
set -uo pipefail
cd "$(dirname "$0")"
PY="${MULTIAGENT_PY:-/home/hermes/.hermes/cache/scratch/oh-venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" lib/secret_scan.py --history "$@"
