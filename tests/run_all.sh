#!/usr/bin/env bash
# One command that runs every acceptance gate in this repo.
# Gate 4 asserts the RC1 repro STILL reproduces against the owner's original
# role files, which is the point: it is a regression detector, not a pass/fail
# on our own shipped roles (team/roles is checked by --plan in gate 3).
# Exits non-zero on the first failure. Costs ZERO API calls.
set -uo pipefail
cd "$(dirname "$0")/.."   # repo root (this script lives in tests/)
V="${MULTIAGENT_PY:-/home/hermes/.hermes/cache/scratch/oh-venv/bin/python}"
[ -x "$V" ] || V="$(command -v python3)"
fail=0
run() {  # label, then the command
  local label="$1"; shift
  printf "\n=== %s\n" "$label"
  if "$@"; then echo "PASS: $label"; else echo "FAIL: $label"; fail=1; fi
}
run "secret scan (tree + git history)" "$V" lib/secret_scan.py --history
run "offline test suite (0 requests)" "$V" -m pytest tests/ -q
run "team plan validation (0 requests)" "$V" team/run_team.py --plan \
      --brief ~/.hermes/qwen-multiagent-brief.md
run "RC1 regression detector still fires (0 requests)" "$V" repro/subagent_tools.py
printf "\n%s\n" "----------------------------------------"
if [ "$fail" -eq 0 ]; then echo "ALL GATES PASS (0 API calls spent)"; else echo "GATES FAILED"; fi
exit $fail
