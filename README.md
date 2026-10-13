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
| `team/teamkit.py` | sanctioned launcher: preflight, run records + lock, pair gate, budget chokepoint, outcome classes |
| `team/run_team.py` + `team/roles/*.md` | orchestrator: architect, parallel fixers, tester, reviewer |
| `team/HANDOFF.md` | brief-in, audit-out procedure |
| `router/` | role, lane, endpoint, budget policy in one file |
| `lib/keys.py` | runtime key resolution + redaction on every write |
| `lib/secret_scan.py` | commit gate: prints `FILE:LINE`, never the secret |
| `lib/ledger.py` | sole writer of the quota ledger (unit: requests) |
| `fix/oh_compat.py` | guards that fail loudly instead of silently |
| `repro/` | investigation scripts + findings |
| `tests/` | 250+ offline tests, zero API calls (`SMOKE_LIVE=1` enables live ones) |
| `docs/` | decision memos, live-run evidence, open incidents |

Model lanes, endpoints and provider names live in local config only.

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
```

`run_team()` grades via `contract={"gate": cmd}`, optionally `"incumbent_gate"`. Outcomes: `GATE_PASS_NEW` (beats incumbent), `GATE_PASS_TIE` (matches), `GATE_FAIL`; failures split by git evidence into `partial_work` vs `abstained` (brief fix vs lane fix). Over-budget calls are refused before spend; `TEAMKIT_DRY_RUN=1` plans free. Every run persists locked records under `runs/<ts>-<slug>/`.

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
