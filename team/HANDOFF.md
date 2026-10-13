# HANDOFF. Brief in, audit out

An agent's own report of success is the least trustworthy artefact here. The audit half matters most.

## 1. Brief in

Brief = markdown file: mission, verified facts, deliverables, constraints.

```bash
cd ~/projects/multiagent-stack
V=/home/hermes/.hermes/cache/scratch/oh-venv/bin/python

$V team/run_team.py --plan    --brief BRIEF.md                        # 0 requests
$V team/run_team.py --dry-run --brief BRIEF.md --workspace REPO       # 0 requests
$V team/run_team.py --brief BRIEF.md --workspace REPO --max-requests 60
```

**Never skip `--plan`.** RC1 cost a full run because a role parsed to zero tools silently; `--plan` builds every sub-agent and refuses any that cannot act, free. A failed plan is cheap; a failed run is not. New lanes go through the probe-first recipe (README) before touching a brief.

Team shape:

```
architect reads BRIEF -> TEAM-SPEC.json {id, role, instruction, paths, acceptance}
2 fixers in parallel  -> disjoint paths only; overlap refused pre-spend
tester                -> runs gates, depends_on fixers
reviewer              -> re-runs every gate, writes DONE.md
```

Each step writes its own `DONE-<id>.md`; a shared `DONE.md` collided on the first live run (`docs/LIVE-RUN.md`).

## 2. Audit out

Ordered by how hard each check is to fake.

**a) Tests actually pass?** Run them yourself in the target workspace: `python3 -m pytest -q`, `compileall`. Exit 0 is evidence; `DONE.md` is a claim.

**b) In budget?** `$V tests/probe_budget.py --since <ts-before-run>` → read `spent_since` vs the per-run ceiling. Do NOT compare against `our_recorded_requests_cumulative` (session-wide; meaningless per-run. This conflation shipped once, then got pinned in tests). Target <120 requests; observed 64.

**c) Secret leak?** `$V lib/secret_scan.py` on working tree and `git log -p` (prints `FILE:LINE` only, so running it can't leak). `runs/` is gitignored but auto-redacted captures are still worth auditing: gitignored is not audited. History lesson: an early interactive probe echoed a live key into terminal scrollback; treat scrollback as not-secret-safe, keys go through `lib.keys`.

**d) Report arithmetic consistent?** Printed `requests_charged` vs ledger delta must agree. Disagreement found the offline-suite quota pollution (30 fictional rows); `tests/test_ledger.py` pins both directions.

**e) Did the plan survive reality?** Check `repro/FINDINGS.md` refuted-hypotheses table. A report with no dead ends is less trustworthy. This repo's biggest finding was that the brief's leading hypothesis was wrong.

## 3. Lane status

`router/router.yaml` is authoritative. Model names are local config, not listed here.

Probe labels: `verified_here` = probed on this machine with captured output; `verified_in_brief` = owner's probes assert it, nobody re-checked. Router refuses `tools_required` lanes below `verified_in_brief`.

Degradation hook: `one_shot_outside_loop: true` on a lane allows endpoint-probe-only use outside tool loops (reason must be commented at the lane). Currently unused; all roles run in-loop. Sub-agent delegation was fixed by #2892 plus SDK auto-registration (`docs/DELEGATION-INCIDENT.md` closed). `team/` works because each role is a direct Conversation from this process.

## 4. Known limits

- **Delegation incident closed 2026-10-13**: Oct-5 `NO_EXEC_TOOLS` was RC1 in our wiring (#2892). SDK auto-registers file agents from project/user `.agents|.openhands/agents`; live probe child received terminal+file_editor. Remaining: validate `team/` map_agents under real spawning.
- **Caps in requests, not dollars**: no litellm price map for these ids, SDK USD guard can never trip; `Team._charge` is the only enforcement that functions.
- `max_budget_per_run` / `prompt_cache_key` need `LocalConversation`; public factory rejects them. Use `router.public_conversation_kwargs()`.
- Empty-turn loop is stochastic (silence or prose escape, both observed); the guard checks the deterministic harm: a crippled agent.
- `runs/` uncommitted; `DONE.md` / `TEAM-SPEC.json` land in the target workspace.
