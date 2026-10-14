# m0. Deterministic gates for delegated coding agents

AI coding agents claim success too often. This harness makes work provable: exit codes decide pass/fail, new work must beat the previous version to count as passing, and zero-touch runs are reported separately from failed-but-trying runs.

Built on OpenHands SDK. Not a fork; upstream fixes go upstream as PRs.

## 4 rules, enforced in code

1. **No calls that do nothing.** A mis-wired team spent dozens of paid calls on agents whose tool list had silently parsed empty ([`repro/FINDINGS.md`](repro/FINDINGS.md)). Preflight refuses any role that cannot act, at zero cost.
2. **Frozen prompt prefixes.** No timestamps or run IDs in opening instructions; a changed first sentence discards the prompt cache.
3. **Plan once, parallelise only across disjoint files.**
4. **Grade with gates + git state.** An agent's report is never evidence.

## Map

| Path | What it is |
|---|---|
| `team/run_team.py` + `team/roles/*.md` | THE engine: architect, parallel fixers, tester, reviewer, then the post-run grading stage (gate, pair check, protected paths, outcome, run record) |
| `team/teamkit.py` | facade over that engine: budget chokepoint, per-slug lock, records, memory loop, agent force-refresh. `run_team()` is still the only sanctioned launcher |
| `team/HANDOFF.md` | brief-in, audit-out procedure |
| `router/` | role, lane, endpoint, budget, request params and the in-loop block list in one file |
| `lib/keys.py` | runtime key resolution + redaction on every write |
| `lib/secret_scan.py` | commit gate: prints `FILE:LINE`, never the secret |
| `lib/ledger.py` | sole writer of the quota ledger (unit: requests) |
| `fix/oh_compat.py` | guards that fail loudly instead of silently |
| `repro/` | investigation scripts + findings |
| `tests/` | 400+ offline tests, zero API calls (`SMOKE_LIVE=1` enables live ones) |
| `docs/` | decision memos, live-run evidence, open incidents |

Model lanes, endpoints and provider names live in local config only.

## One engine

Two runners used to exist: the orchestrator had no gates, and the gated launcher had never been validated end to end. They are one now. `Team.run_workflow()` runs the work and then grades it, and `teamkit.run_team()` delegates to it while keeping everything its callers depend on: the return keys, the chokepoints, the lock, the record shape, the memory loop.

`router.yaml` is authoritative on both paths. Lane config (endpoint, temperature, `drop_params`, retries, `capability_overrides`, iteration cap) reaches the SDK through the router, a `model=` argument may replace only the model id, and the in-loop block list is the declarative `defaults.inloop_blocked_prefixes` with the old tuple kept as a fallback for a config that lacks the key.

## Statuses and exit codes

Grading happens after the workflow, never inside an agent prompt. Gate commands run with a 900s timeout and the workspace as cwd.

| Status | Meaning | When |
|---|---|---|
| `GATE_PASS` | gate exited 0 | legacy path: no `incumbent_gate` in the contract |
| `GATE_PASS_NEW` | gate exited 0 and the incumbent did not | improvement is proven |
| `GATE_PASS_TIE` | gate and incumbent both exited 0, or the pair check could not run | kept, not proven better |
| `GATE_FAIL` | gate exited non-zero, **or** a protected path was tampered with | the incumbent is retained |
| `RUN_DONE_NO_GATE` | the run finished and the contract has no gate | nothing to grade |

`status` plus `gate`, `gate_tail`, `pair`, `outcome`, `work_evidence` and `run_dir` are added to the report dict. Existing keys are unchanged.

The outcome split reads git evidence captured before the gate runs, so a gate that executes a test suite cannot be credited with the `__pycache__` dirt it creates: `success`, `tie_kept`, `partial_work` (real edits, gate failed), `abstained` (zero commits, zero diff), `refused` (never spent).

`team/run_team.py` exit codes:

| Exit | Meaning |
|---|---|
| 0 | `GATE_PASS` or `GATE_PASS_NEW`, no failed step |
| 1 | `GATE_FAIL`, or a pass-family verdict with a failed step |
| 2 | `GATE_PASS_TIE`, and every refuse-before-spend path (bad config, missing brief, ceiling above the router's) |
| 3 | a graded run that aborted mid-flight |

`--dry-run` keeps its own contract: it prints the plan, spends nothing, and grades nothing.

## Protected paths

An agent that rewrites the test it was supposed to satisfy has fixed nothing, and a gate run against its own edited tests says `GATE_PASS`. `contract={"protect": ["tests/"], ...}` inverts that. After the run and before the gate, every path the run changed (committed diff since the run-start HEAD plus new working dirt) must fall outside each protected entry or be covered by a task's own declared paths. One violation means `GATE_FAIL` with `reason: protected-path-tampered:<file>` regardless of the gate's exit code.

Matching is containment, resolved with `os.path.realpath` and `commonpath` in one helper shared with the parallel-task overlap check. `tests/` protects `tests/test_x.py`, `./tests/` normalises to the same thing, and `pkg` does not protect `pkgfoo.py`. That helper is also why `tests/` and `tests/test_x.py` no longer count as disjoint: exact-string comparison said they were, and the torn write only surfaced in someone else's test run. No `protect` key means no check, so nothing that ran before this change behaves differently.


## Quick start

Test commands are exact, because the interpreter is load-bearing:

* **pytest runs on bare `/usr/bin/python3`.** The repo-root `conftest.py` appends the SDK venv's site-packages, so system python (which has pytest) can import `openhands`. Observed live, the same run on the venv python died instantly with "No module named pytest", so `tests/test_e2e_smoke.py` refuses any interpreter but system python under `SMOKE_LIVE` and prints the working command (`MULTIAGENT_PY` opts out of the check).
* **the `team/run_team.py` CLI needs an interpreter that can import the SDK**, so it takes the venv python, or system python with `PYTHONPATH` pointed at the venv's site-packages. The conftest shim is pytest-only.

```bash
PY=/usr/bin/python3                              # system python: the pytest interpreter
$PY -m pytest tests/ -q                                   # 0 calls
$PY -m pytest tests/test_e2e_smoke.py -q                  # 0 calls (offline smoke)
SMOKE_LIVE=1 $PY -m pytest tests/test_e2e_smoke.py -q -s  # live smoke: real run, spends requests

V=<venv-with-openhands-sdk>/bin/python           # SDK interpreter: the CLI runner
$V team/run_team.py --plan    --brief BRIEF.md    # 0 calls
$V team/run_team.py --dry-run --brief BRIEF.md --workspace REPO
$V team/run_team.py --brief BRIEF.md --workspace REPO --max-requests 60
$V team/run_team.py --brief BRIEF.md --workspace REPO --gate "pytest -q tests/" \
    --incumbent-gate "pytest -q tests/" --protect tests/    # graded run
```

Grading is the same on both entry points: the CLI passes `--gate/--incumbent-gate/--protect`, `run_team()` passes `contract={"gate": cmd, "incumbent_gate": cmd, "protect": [paths]}`. Over-budget calls are refused before spend. `TEAMKIT_DRY_RUN=1` plans free. Every graded run persists a record under `runs/<ts>-<slug>/`, written by whichever entry point ran it.

`run_team()` exports `TASK_START_TS=<run-start epoch>` before launching anything, so the gate and every agent subprocess share one window and `team/roles/tester.md` can report spend *since this run started* instead of since the session began; the value is recorded as `task_start_ts` in `config.json`. `config.json` also carries `recall` (`{recall_status: fired|empty|unavailable|error, hits: [{name|sha8, chars}]}`): attribution for the memory block appended to the brief, with `unavailable` recorded explicitly rather than left as silence. And `agents`: which file agents were force-refreshed into the registry, their effective iteration caps (`min(declared, budget_calls*3)`), and any `model:` value healed to `inherit`.

## Adding a model lane

Probe first, config second. Never copy an existing lane's config:

```bash
$V repro/capture_request.py        # outbound body, 0 requests
./repro/raw_curl.sh both           # endpoints raw, 2 requests
$V repro/ab_probe.py --lane both   # litellm layer
$V repro/sdk_conversation.py       # one real agent turn
```

Then edit `router/router.yaml`. Its `probes:` block demands provenance; unprobed tool-calling lanes cannot take traffic.

## Limitations

- Sub-agent delegation incident (2026-10-05) closed 2026-10-13: root cause was our own frontmatter wiring; live probe confirms children get terminal + file_editor. `team/` map_agents still untested end-to-end under real spawning (`docs/DELEGATION-INCIDENT.md`).
- Cost tracked in requests, not dollars (no price map for these lanes).
- Model-authored commands are recorded, never auto-executed by the harness.
- Memory (TDAI): the atomic half of recall is empty by construction. `/v3/conversation/add` ingestion does not exist yet, so `/v3/atomic/search` can only ever return nothing. The skill-asset half is the live path. See `team/tdai-bridge.py`.
- Memory (TDAI): the bridge is two files that must stay byte-identical, `team/tdai-bridge.py` (repo-owned, imported by teamkit) and `~/.hermes/scripts/tdai-bridge.py` (executor-readable, used by scripts outside this repo). `tests/test_tdai_bridge.py` fails on drift.

## Reading order

1. `repro/FINDINGS.md`: root cause + refuted hypotheses
2. `docs/LIVE-RUN.md`: end-to-end proof with numbers
3. `team/HANDOFF.md`: usage + audit
4. `docs/DELEGATION-INCIDENT.md`: what still fails
