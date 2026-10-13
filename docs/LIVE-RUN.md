# Live run evidence — the team workflow against real endpoints

Date: 2026-10-05 · Model lanes: `anthropic/glm-5.3` (architect),
`openai/qwen3.8-flash` (fixers/tester), `openai/qwen3.8-max` (reviewer).
Reproduce: `SMOKE_LIVE=1 python3 -m pytest tests/test_e2e_smoke.py -q -s`

This is the raw report from the successful end-to-end run. Nothing here is
reconstructed — it is pasted from the captured stdout of
`tests/test_e2e_smoke.py::test_live_team_workflow_fixes_the_repo`, with long
action lists elided.

## Result

```
--- SMOKE REPORT ---
  "requests_charged": 64,
  "spec_source": "file",
  "tasks_parsed": 3,
  "cleared_stale_status_files": [],
  "status_files": ["DONE-1.md", "DONE-2.md", "DONE-spec.md", "DONE.md"]

  spec     architect  ok=True  req=9   err=None
  1        fixer      ok=True  req=7   err=None
  2        fixer      ok=True  req=16  err=None
  3        tester     ok=True  req=20  err=None
  review   reviewer   ok=True  req=12  err=None

  ledger delta: 64 request(s); ceiling 100
  gates: {"durations": "ok", "normalise": "ok", "whole": "ok", "compile": "ok"}
```

`PYTEST_EXIT:0` for the file. Actions per step (elided above) were real tool
calls: the architect emitted `FileEditorAction`/`TerminalAction`/`FinishAction`;
the tester ran 26 `TerminalAction`s; the reviewer 20.

## What this proves, and what it does not

Proves:
- The full architect → 2 parallel fixers → dependent tester → reviewer chain
  runs against the live endpoints and makes the target repo's tests go green.
- **64 requests for that run — inside the brief's <120 target.** The three live
  tests in the file together spent 139, which is why `probe_budget` reports a
  time window and a cumulative total separately.
- Cache holds under a real team: the fixer turn reported **93.9% cache hit**.
- Per-task `DONE-<id>.md` files were produced, one per step, no collision.
- The dependency ordering held: task 3 (tester) ran only after the two fixers.

Does NOT prove:
- **No sub-agent delegation was used.** Every step here is one `Conversation`
  driven by this orchestrator process, not a spawned sub-agent. The `task` tool
  defect in `docs/DELEGATION-INCIDENT.md` is unresolved, so the brief's
  "OpenHands orchestrator spawning sub-agents" shape is demonstrated in
  structure (role files, lanes, parallel waves, handoff contract) but not via a
  live sub-agent tree.

## The path to this run was two real bugs, both mine

1. **HTTP 400 on `openai/qwen3.8-flash`** — my router set
   `extra_body.enable_thinking=false` while the SDK kept injecting
   `reasoning_effort='high'`; the endpoint requires the pair to agree. Diagnosed
   with `repro/capture_request.py` at **zero API calls** and now refused at
   config-load time by `router._check_thinking_consistency` — which immediately
   caught the same mistake on the tester lane, which I had not spotted.
2. **`text_len: 0` on every step** — an acting turn emits no `MessageEvent` with
   `source=agent`, so the architect's reply was invisible and the workflow fell
   back to zero parsed tasks. The first "successful" run quietly did nothing
   except write a spec file; the fix captures `FinishAction.message`.

Worth noting about failure mode 2: `run_workflow` **warned and skipped fixers**
rather than reporting success. That is the designed behaviour, and it is the only
reason the bug surfaced in the report instead of passing as a green run.

## Cost profile (from the ledger)

| model | requests | role |
|---|---|---|
| `openai/qwen3.8-flash` | 123 | fixers / tester / orchestrator |
| `anthropic/glm-5.3` | 17 | architect |
| `openai/qwen3.8-max` | 12 | reviewer |
| `openai/glm-5.3` | 2 | transport A/B probes |

Cumulative across the whole session (diagnosis + 3 live tests). The
diagnosis phase — kwargs dump, role-file repro, request capture, bisecting the
400 — cost **0 requests**, which is the single biggest budget win in this repo
and the reason `capture_request.py` and `--dry-run` exist.
