# LIVE-RUN. End-to-end proof against real endpoints

Date: 2026-10-05 · Reproduce: `SMOKE_LIVE=1 python3 -m pytest tests/test_e2e_smoke.py -q -s`
Pasted from captured stdout of `test_live_team_workflow_fixes_the_repo`, action lists elided.

## Result

```
--- SMOKE REPORT ---
  "requests_charged": 64,
  "spec_source": "file",
  "tasks_parsed": 3,
  "status_files": ["DONE-1.md", "DONE-2.md", "DONE-spec.md", "DONE.md"]

  spec     architect  ok=True  req=9   err=None
  1        fixer      ok=True  req=7   err=None
  2        fixer      ok=True  req=16  err=None
  3        tester     ok=True  req=20  err=None
  review   reviewer   ok=True  req=12  err=None

  ledger delta: 64 request(s); ceiling 100
  gates: {"durations": "ok", "normalise": "ok", "whole": "ok", "compile": "ok"}
```

Actions were real tool calls (tester ran 26 `TerminalAction`s, reviewer 20).

## Proves

- Full chain (architect → 2 parallel fixers → dependent tester → reviewer) green against live endpoints; target repo's tests pass.
- 64 requests vs <120 target. All three live tests together spent 139. Hence `probe_budget` separates time-window from cumulative.
- 93.9% cache hit on the fixer turn.
- Per-task `DONE-<id>.md`, no collision; dependency ordering held.

## Does NOT prove

- No sub-agent delegation: every step is one direct `Conversation`. The `task` defect (`docs/DELEGATION-INCIDENT.md`) is unresolved, so the spawn-tree shape is demonstrated in structure only.

## Two bugs found on the way (both mine)

1. **HTTP 400**: router set `enable_thinking=false` while SDK injected `reasoning_effort='high'`; endpoint requires agreement. Diagnosed at **0 API calls** via `capture_request.py`; now refused at config load by `router._check_thinking_consistency`, which immediately caught the same mistake on the tester lane.
2. **`text_len: 0`**: acting turns emit no agent `MessageEvent`, so replies were invisible and tasks parsed zero. success. `run_workflow` warned and skipped rather than claiming success, which is designed behaviour, and the only reason this surfaced instead of passing green.

## Session cost profile (ledger)

| model | requests | role |
|---|---|---|
| `<lane>` | 123 | fixers / tester / orchestrator |
| `<lane>` | 17 | architect |
| `<lane>` | 12 | reviewer |
| `<lane>` | 2 | transport A/B probes |

Diagnosis phase (kwargs dump, role-file repro, 400 bisect): **0 requests**. Biggest budget win here, and the reason `capture_request.py` + `--dry-run` exist.
