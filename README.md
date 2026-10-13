# m0. Deterministic gates for delegated coding agents

AI coding agents claim success far too often. This harness makes their work provable. A check command's exit code decides pass or fail. Results are compared against the previous version of the code, so "tests pass" only counts when the new work beats what existed. Runs that touched nothing are reported separately from runs that tried and failed.

Built on top of separate upstream projects (OpenHands SDK). Not a fork. Improvements meant for upstreams go there as pull requests.

## 4 rules, enforced in code

1. **Don't make calls that do nothing.** A mis-wired team once spent dozens of paid calls on agents whose tool list had silently parsed empty. They could not act and said nothing, 12 times in a row. Preflight now refuses any role that could not act, and it costs zero requests to run. ([`repro/FINDINGS.md`](repro/FINDINGS.md))
2. **Never make the worker re-read the blueprint.** Opening instructions are frozen byte-for-byte: no timestamps, no random run IDs in prompts. A changed first sentence discards the prompt cache and you pay for the re-read.
3. **Batch the thinking, then parallelise the doing.** One planner, then parallel workers assigned disjoint files only.
4. **Check the work yourself.** Gate commands and git state grade every run. An agent's own report is never evidence.

## Map

| Path | What it is |
|---|---|
| `team/teamkit.py` | sanctioned launcher: lane preflight, run records + lock, pair gate, budget chokepoint, outcome classes |
| `team/run_team.py` + `team/roles/*.md` | orchestrator: architect, parallel fixers, reviewer |
| `team/HANDOFF.md` | how to brief in, and how to audit what comes back |
| `router/` | role, model-lane, endpoint and budget policy in one auditable file |
| `lib/keys.py` | key resolution without keys ever entering a file, redaction on every write |
| `lib/secret_scan.py` | commit gate: prints `FILE:LINE`, never the secret |
| `lib/ledger.py` | sole writer of the quota ledger (requests are the budget unit) |
| `fix/oh_compat.py` | guards that fail loudly instead of silently |
| `repro/` | investigation scripts + root-cause findings |
| `tests/` | 250+ offline tests, zero API calls. Live smokes skip unless `SMOKE_LIVE=1` |
| `docs/` | decision memos, live-run evidence, open incidents |

Model lanes, endpoints and provider names live in local config only. This repo
is interface and discipline, not credentials or vendor choices.

## Quick start

```bash
cd ~/projects/multiagent-stack
V=<venv-with-openhands-sdk>/bin/python

$V -m pytest tests/ -q                            # zero API calls
$V team/run_team.py --plan    --brief BRIEF.md    # zero calls: every role actually gets tools
$V team/run_team.py --dry-run --brief BRIEF.md --workspace REPO
$V team/run_team.py --brief BRIEF.md --workspace REPO --max-requests 60
```

`run_team()` grades with `contract={"gate": cmd}`, optionally plus
`"incumbent_gate": cmd`. With the incumbent gate, passing means beating the
previous version (`GATE_PASS_NEW`). Merely matching is `GATE_PASS_TIE`. Failures
split by git evidence into `partial_work` vs `abstained` in the `outcome` field.
A stall needs a lane fix, a failed gate after real edits needs a better brief.
Budgets above the ceiling are refused before any spend, and `TEAMKIT_DRY_RUN=1`
plans a run at zero cost. Every run persists `runs/<ts>-<slug>/` records
(config, stage state, summary, retained-pair decision) under a pid lock.

## Adding a model lane

Probe first, config second. Never copy an existing lane's config:

```bash
$V repro/capture_request.py        # exact outbound request body, 0 requests
./repro/raw_curl.sh both           # endpoints directly, 2 requests
$V repro/ab_probe.py --lane both   # SDK layer
$V repro/sdk_conversation.py       # one real agent turn
```

Only then edit `router/router.yaml`. Its `probes:` block demands provenance you
can stand behind: a tool-calling lane nobody probed cannot take traffic.

## Limitations

- Sub-agent delegation has an unresolved tool-registration defect (`docs/DELEGATION-INCIDENT.md`). "Multi-agent" here means one orchestrator over parallel role-scoped conversations where files are disjoint.
- Cost is tracked in requests, not dollars. No price map exists for these lanes, and requests are the unit subscriptions actually bill.
- Model-authored "acceptance commands" are recorded, never auto-executed by the harness. Running them stays a human/reviewer action, by design.

## Reading order

1. `repro/FINDINGS.md`: what was wrong, and the hypotheses that died first
2. `docs/LIVE-RUN.md`: proof the fixed thing works, with real numbers
3. `team/HANDOFF.md`: usage and audit procedure
4. `docs/DELEGATION-INCIDENT.md`: what still doesn't work
