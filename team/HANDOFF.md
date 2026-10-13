# HANDOFF. How Hermes gives this team a brief, and how to audit what comes back

Two directions: **in** (a brief to the team) and **out** (evidence to an audit that either signs it off or sends it back). The out half is the important one: an agent's own report of success is the least trustworthy artefact in the system.

---

## 1. Hand a brief in

A brief is a markdown file with a mission, verified facts, deliverables, and constraints. The one this repo was built from is `~/.hermes/qwen-multiagent-brief.md`.

```bash
cd ~/projects/multiagent-stack
V=/home/hermes/.hermes/cache/scratch/oh-venv/bin/python

# STEP 1: costs 0 requests, and catches the failures that cost the most.
$V team/run_team.py --plan --brief /path/to/BRIEF.md

# STEP 2: same, but walks the whole workflow with fake responses. Still 0.
$V team/run_team.py --dry-run --brief /path/to/BRIEF.md --workspace /path/to/repo

# STEP 3: the real thing, with an explicit ceiling below the configured one.
$V team/run_team.py --brief /path/to/BRIEF.md --workspace /path/to/repo \
    --max-requests 60
```

**Do not skip step 1.** RC1 (see `repro/FINDINGS.md`) cost an entire netflow run because a role file parsed to zero tools and nothing raised. `--plan` runs the same assertion for free: it builds every sub-agent and refuses any that could not act. A failed `--plan` is a cheap failure. A failed run is not.

Before a new model lane goes anywhere near a brief, run the probe-first recipe (`README.md`, "Adding a model lane"). Never wire a model in on the strength of a hunch about what it "probably" supports.

### What the team actually does with it

```
architect  (<lane>)   reads BRIEF.md -> writes TEAM-SPEC.json
   |                              {id, role, instruction, paths, acceptance}
fixers  -- parallel --           disjoint paths only; the orchestrator refuses
fixers  -- parallel --           an overlap before spending anything
   |
tester     (<lane>) runs the gates, depends_on the fixers
   |
reviewer   (<lane>)   re-runs every gate, writes DONE.md
```

Each step also writes its own `DONE-<id>.md`. Per-task, not shared: a single `DONE.md` is a collision two parallel fixers will hit (they did, on the first live run, see `docs/LIVE-RUN.md`).

---

## 2. Audit what comes back

Audit order is by how hard each check is to fake.

### a) Did the tests actually pass? Run them yourself.

Never read the agent's summary as the verdict. In the target workspace:

```bash
cd /path/to/repo && python3 -m pytest -q
python3 -m compileall -q pkg          # or the repo's own build/lint gate
```

Expect exit 0. `DONE.md` is a claim. A green pytest you ran yourself is evidence.

### b) Did it stay in budget? Check the ledger, per run.

```bash
$V tests/probe_budget.py --since <unix-time-before-the-run>
```

The number that matters is `spent_since`, compared against the per-run ceiling. `our_recorded_requests_cumulative` is *every* run this session and will exceed the per-run ceiling the moment there has been more than one. That comparison is meaningless, and conflating the two is a bug this repo actually shipped and then fixed (see `docs/LIVE-RUN.md`).

Brief target for a run shaped like this: **under 120 requests.** Observed: 64.

### c) Did anything secret leak?

```bash
$V lib/secret_scan.py                      # working tree
$V lib/secret_scan.py && git log -p -- . | $V lib/secret_scan.py /dev/stdin
```

Exit 0 and nothing credential-shaped. The scanner prints `FILE:LINE` only, so running it cannot itself leak. Note that `runs/` is gitignored. Captures are auto-redacted on write, but gitignored is not the same as audited.

One caveat this repo has already earned: an interactive probe of `~/.hermes/config.yaml` early in the investigation echoed the live key into the terminal scrollback. Not in any file, commit, or captured artefact. But scroll history is not a store you treat as secret-safe. Keys go through `lib.keys` and are never printed.

### d) Is the report's own arithmetic consistent?

Compare what `run_team.py` printed against the ledger delta. They should agree. If a run claims `requests_charged: 0` while the ledger moved, accounting is broken somewhere. That is precisely how the offline test-suite pollution was found (30 rows of fictional requests), and `tests/test_ledger.py` now pins both directions of that gate.

### e) Did the plan survive contact with reality?

Skim `repro/FINDINGS.md` section 4 (refuted hypotheses). A report that lists only confirmed hypotheses and no dead ends is less trustworthy, not more. The brief asked for failed hypotheses to be recorded, and this repo's most important finding is that the brief's own leading hypothesis (thinking-model handling) was wrong.

---

## 3. Lane status (router.yaml is authoritative; this is the commentary)

| role | lane | transport | in-loop? | probe status |
|---|---|---|---|---|
| architect | architect | apps/anthropic | yes | verified_here |
| research | architect | apps/anthropic | yes | verified_here |
| fallback_architect | fallback_architect | apps/anthropic | yes | verified_in_brief |
| fixer | implementer | compatible-mode | yes | verified_here (after the 400 fix) |
| implementer | implementer | compatible-mode | yes | verified_here |
| tester | tester | compatible-mode | yes | verified_in_brief |
| reviewer | reviewer | compatible-mode | yes | verified_in_brief |
| orchestrator | implementer | compatible-mode | yes | verified_here |

Model names are local config, deliberately not listed here.

`verified_here` = probed on this machine, today, output captured.
`verified_in_brief` = the owner's live probes assert it; nobody re-checked here.
The router refuses a `tools_required` lane whose `raw_curl`/`litellm` are not at least `verified_in_brief`.

### Degradation path (required by the brief)

If a lane cannot be trusted inside a tool loop, set on that lane:

```yaml
one_shot_outside_loop: true
```

The router will then allow it with only an endpoint probe, and the reason must be written into `router.yaml` as a comment next to it. **No lane is currently on this path**: every role in `router.yaml` runs in-loop. The mechanism exists because the brief demands graceful degradation, and it was load-bearing during diagnosis. It is what you reach for if a model turns out not to survive multi-turn tools.

The genuine open degradation is elsewhere: **sub-agent delegation does not work** (`docs/DELEGATION-INCIDENT.md`). `team/` runs each role as a direct Conversation from this process, which works. Spawning a sub-agent tree from within OpenHands does not, and the cause is not RC1.

---

## 4. Known limits

- **Delegation is broken.** The `task` tool hands sub-agents only `finish`/`think`. Tested against RC1 and refuted; it is a separate tool-registration defect. Until it is fixed, "multi-agent" here means one orchestrator process driving several role-scoped conversations, in parallel where the files are disjoint. That is stated plainly and it works. It is not what the brief pictured.
- **Budget caps are in requests, not dollars.** litellm has no price map for these model ids ("This model isn't mapped yet"), so accumulated cost reads $0.00 and the SDK's `max_budget_per_run` guard can never trip. `Team._charge` is therefore the only budget enforcement that actually functions.
- **`max_budget_per_run` / `prompt_cache_key` need `LocalConversation`.** The public `Conversation` factory rejects them. `router.public_conversation_kwargs` exists so callers do not find out at runtime.
- **The empty-turn loop is stochastic.** With an empty tool set the model sometimes emits nothing and sometimes escapes with prose (both observed). The deterministic harm is a crippled agent, which is what the guard checks.
- **`runs/` is not committed** (`.gitignore`), and `DONE.md` / `TEAM-SPEC.json` land in the *target* workspace.
