#!/usr/bin/env python3
"""probe_budget — count API requests this repo has spent, and check the ceiling.

The plan bills REQUESTS (5h / week / month windows), not tokens, so "did the run
fit its budget?" is a request question. tokenwatch.py is the ledger; this reads
it, diffs it, and enforces the ceiling declared in router.yaml.

    python3 tests/probe_budget.py                     # snapshot + verdict
    python3 tests/probe_budget.py --record glm-5.3 3  # add 3 requests
    python3 tests/probe_budget.py --delta             # print ledger line count
    python3 tests/probe_budget.py --check 100         # fail if over 100

`--record` is what team/run_team.py and repro/*.py use indirectly (they call
tokenwatch.record() themselves); it is here so a manual probe can be logged
without editing code.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEDGER = Path.home() / ".hermes" / "quota" / "calls.jsonl"
TOKENWATCH = Path.home() / ".hermes" / "scripts" / "tokenwatch.py"
PREFIX = "multiagent-stack"


def _load_tokenwatch():
    if not TOKENWATCH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("tokenwatch", TOKENWATCH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows(prefix: str = PREFIX) -> list[dict]:
    if not LEDGER.is_file():
        return []
    out = []
    for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("source", "")).startswith(prefix):
            out.append(row)
    return out


def total(prefix: str = PREFIX) -> int:
    return sum(int(r.get("calls", 1)) for r in rows(prefix))


def by_model(prefix: str = PREFIX) -> dict[str, int]:
    tally: dict[str, int] = {}
    for row in rows(prefix):
        tally[row.get("model", "?")] = tally.get(row.get("model", "?"), 0) + int(
            row.get("calls", 1))
    return tally


def ceiling_from_router() -> int:
    """The per-team-run ceiling configured in router.yaml (single source)."""
    try:
        sys.path.insert(0, str(REPO))
        from router import Router

        return int(Router.load().budget.get("max_requests_per_team_run", 100))
    except Exception:
        return 100


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", nargs=2, metavar=("MODEL", "CALLS"),
                    help="append N requests for MODEL to the ledger")
    ap.add_argument("--source", default=f"{PREFIX}:manual")
    ap.add_argument("--delta", action="store_true",
                    help="print the current ledger line count (use before/after)")
    ap.add_argument("--check", type=int, default=None,
                    help="exit 1 if our recorded total exceeds N")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--since", type=float, default=None,
                    help="only count ledger rows at/after this unix time "
                         "(use a value captured before the run started)")
    args = ap.parse_args(argv)

    if args.record:
        model, calls = args.record
        module = _load_tokenwatch()
        if module is None:
            print(f"tokenwatch not found at {TOKENWATCH}", file=sys.stderr)
            return 2
        module.record(args.source, model, int(calls))
        print(f"recorded {calls} request(s) for {model} under {args.source}")
        return 0

    mine = total()
    ledger_lines = len(LEDGER.read_text(errors="replace").splitlines()) if LEDGER.is_file() else 0
    if args.delta:
        print(ledger_lines)
        return 0

    # Cumulative-vs-per-run distinction, because conflating them lies about
    # budget: the ceiling in router.yaml caps ONE team run, but the ledger has
    # every run this session. After three live smoke runs the cumulative total
    # (154) exceeds the per-run ceiling (100) while no single run ever did.
    since = args.since if args.since is not None else 0.0
    window_rows = [r for r in rows() if float(r.get("ts", 0)) >= since]
    spent = sum(int(r.get("calls", 1)) for r in window_rows)
    ceiling = args.check if args.check is not None else ceiling_from_router()
    report = {
        "ledger": str(LEDGER),
        "ledger_lines": ledger_lines,
        "our_recorded_requests_cumulative": mine,
        "spent_since": spent,
        "since": since or "session start",
        "by_model": by_model(),
        "per_run_ceiling": ceiling,
        "within_ceiling": spent <= ceiling,
        "note": ("within_ceiling compares the --since window to the per-run "
                 "ceiling; cumulative totals will always exceed it once more "
                 "than one run has happened"),
        "windows": None,
    }
    module = _load_tokenwatch()
    if module is not None:
        try:
            report["windows"] = module.snapshot()
        except Exception as exc:  # pragma: no cover
            report["windows"] = f"snapshot failed: {type(exc).__name__}"

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"this window         : {spent} request(s) vs per-run ceiling {ceiling}")
        print(f"cumulative (session): {mine} request(s)")
        for model, n in sorted(report["by_model"].items(), key=lambda kv: -kv[1]):
            print(f"   {model:32s} {n}")
        win = report.get("windows") or {}
        for name, data in (win.get("windows") or {}).items():
            print(f"plan window {name:12s}: {data['used']}/{data['cap']} "
                  f"({data['pct']}%)")
        print("NOTE: local counts only — the provider console is the source of truth.")
        if not report["within_ceiling"]:
            print(f"OVER CEILING: this window spent {spent} > {ceiling}")

    if args.check is not None and spent > args.check:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
