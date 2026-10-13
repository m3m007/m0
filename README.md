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

```bash
V=<venv-with-openhands-sdk>/bin/python
$V -m pytest tests/ -q                            # 0 calls
$V team/run_team.py --plan    --brief BRIEF.md    # 0 calls
$V team/run_team.py --dry-run --brief BRIEF.md --workspace REPO
$V team/run_team.py --brief BRIEF.md --workspace REPO --max-requests 60
```

`run_team()` grades via `contract={"gate": cmd}`, optionally `"incumbent_gate"`. Outcomes: `GATE_PASS_NEW` (beats incumbent), `GATE_PASS_TIE` (matches), `GATE_FAIL`; failures split by git evidence into `partial_work` vs `abstained` (brief fix vs lane fix). Over-budget calls are refused before spend; `TEAMKIT_DRY_RUN=1` plans free. Every run persists locked records under `runs/<ts>-<slug>/`.

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

## Reading order

1. `repro/FINDINGS.md`: root cause + refuted hypotheses
2. `docs/LIVE-RUN.md`: end-to-end proof with numbers
3. `team/HANDOFF.md`: usage + audit
4. `docs/DELEGATION-INCIDENT.md`: what still fails
