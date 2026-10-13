# multiagent-stack

A small, careful way to run a team of AI coding agents on subscriptions the owner
already pays for — Alibaba **Token Plan** as the main lane, **Kimi/Moonshot** as a
short-lived backup — without burning money, without leaking keys, and without
pretending a model failed when our own wiring did.

Everything here is local. `git init`, no GitHub push, no new paid service.

---

## The whole thing, explained like you're 15

Imagine you hire a contractor and pay them **per phone call**, not per hour.

You have one phone number that reaches three different workers (call them the
Architect, the Fixer, and the Reviewer). Each call costs the same-ish, but a call
where the worker has to re-read the whole blueprint from scratch costs *five times*
as much, because you pay for the re-reading too.

So the entire game is:

1. **Don't make calls that do nothing.** A mis-wired team once spent dozens of
   calls on agents that had no tools and could not do anything. The fix cost zero
   calls to find, and it was our bug, not the worker's.
2. **Never make the worker re-read the blueprint.** If your first sentence changes
   by even one character, they throw away their notes and start over — and you pay
   for the re-read. So the opening instructions are *frozen*. No timestamps, no
   "today is Tuesday", no random run IDs in the prompt.
3. **Batch the thinking, then parallelise the doing.** One Architect plans. Then
   two Fixers work at the same time — *only* because they were assigned different
   files. If they were assigned the same file, we'd pay to watch them overwrite
   each other.
4. **Check the work yourself.** When the team says "done", you run the tests
   yourself. Agents are optimistic.

That's it. Everything in this repo exists to enforce those four rules — and the
enforcement is in code, not in a document that says "please remember to".

**What we found out** (this is the interesting part): the bug everyone was hunting
— "the model returns empty responses, the thinking-model support must be broken" —
was real in its symptoms and completely wrong in its diagnosis. The endpoint was
fine. The model was fine. Our team's worker files had a title line one row too high,
which made the tool list silently parse as *empty*, so the agent could do nothing
and said nothing, 12 times in a row. Details in [`repro/FINDINGS.md`](repro/FINDINGS.md).

---

## Map

| Path | What it is |
|---|---|
| `lib/keys.py` | get a key without ever putting it in a file; `redact()` anything before it's saved |
| `lib/secret_scan.py` | the commit gate: prints `FILE:LINE`, never the secret |
| `lib/ledger.py` | the **only** thing allowed to write the quota ledger |
| `repro/` | the investigation: scripts + `FINDINGS.md` (root cause, and the hypotheses that died) |
| `fix/oh_compat.py` | correct wiring + guards that fail loudly instead of silently |
| `router/router.py` + `router.yaml` | role → model → endpoint → budget, in one auditable place |
| `team/run_team.py` + `team/roles/*.md` | the orchestrator: architect → parallel fixers → reviewer |
| `team/HANDOFF.md` | how to hand a brief in, and how to audit what comes back |
| `team/teamkit.py` | the sanctioned launcher + run records + pair gate + budget chokepoint (see below) |
| `conftest.py` (repo root) | makes the SDK in the shared venv importable so the gate interpreter can run the whole suite |
| `tests/` | 212 offline tests (0 requests) + 3 live smoke tests that skip unless `SMOKE_LIVE=1` |
| `docs/` | `PLAN.md`, `LIVE-RUN.md` (real evidence), `DELEGATION-INCIDENT.md` (what's still broken) |
| `runs/` | captured request/response JSON. **gitignored**, always written redacted |

---

## Quick start

```bash
cd ~/projects/multiagent-stack
V=/home/hermes/.hermes/cache/scratch/oh-venv/bin/python   # has openhands-sdk 1.51 + litellm

$V -m pytest tests/ -q            # 212 pass, 3 live skipped: ZERO API calls
$V lib/keys.py                    # proves key resolution without printing the key
$V team/run_team.py --plan --brief ~/.hermes/qwen-multiagent-brief.md   # 0 calls
$V team/run_team.py --dry-run --brief BRIEF.md --workspace /path/to/repo # 0 calls
```

Then, when `--plan` and `--dry-run` are clean:

```bash
$V team/run_team.py --brief BRIEF.md --workspace /path/to/repo --max-requests 60
SMOKE_LIVE=1 $V -m pytest tests/test_e2e_smoke.py -q -s     # the real e2e run
```

Every live thing prints its own cost. `probe_budget.py` reads the ledger back.

`/usr/bin/python3 -m pytest tests/ -q` (the bare system interpreter, which is
what the acceptance gate uses) works too: the repo-root `conftest.py` appends the
shared venv's site-packages **only when `openhands` is otherwise unimportable**,
so the suite runs complete instead of erroring on ~40 SDK-dependent tests. It
adds nothing when the SDK is already present, and it never hides a failure — if
the venv is gone, those tests fail exactly as they did before.

---

## When to use which lane

Cost is measured in **requests** (5-hour / weekly / monthly caps on this plan), so
"cheaper" mostly means "fewer round trips" and "better cache hits".

| Situation | Use | Why |
|---|---|---|
| Plan, spec, root-cause analysis, adversarial review | `architect` → `anthropic/glm-5.3` | verified here: real tool calls + 93% cache hit; the frontier lane works in-loop |
| Routine edits, refactors, mechanical fixes | `implementer`/`fixer` → `openai/qwen3.8-flash` | cheap, fast, parallel-safe; the workhorse |
| Running tests / builds / lints | `tester` → `openai/qwen3.8-flash` | same lane, low thinking budget |
| PASS/FAIL verdict against gates | `reviewer` → `openai/qwen3.8-max` | thinking on, single-shot-ish, doesn't need tools |
| Architect unavailable / degraded | `fallback_architect` → `anthropic/deepseek-v4-pro` | probed in the owner's brief, not re-probed here |
| Everything on Token Plan is down and it's before ~2026-10-10 | `kimi_backup` → `openai/kimi-for-coding` | time-limited subscription; the router **refuses** it after its `expires:` date |
| You are not sure a lane works | **none — probe first** | see the recipe below |

Two rules that outrank the table:

- **Never conclude "model can't do X" without probing both endpoints.** The
  investigation that produced this repo started from a confident version of that
  sentence and it was wrong.
- **Never add a lane by copying an existing one's config.** Copy the *probe*, then
  the config.

---

## Probe-first recipe: adding a model lane safely

Do this **before** editing `router.yaml`. Steps 1–2 cost 0 requests; you only pay
once the config is already known-good.

### Step 1 — ask what we'd actually send (0 requests)

```bash
$V repro/show_kwargs.py --json
$V repro/capture_request.py          # captures the full outbound body, sends nothing
```

`capture_request.py` wraps `litellm.completion` and raises *before* the network
call, so you read the exact body the SDK would send. This is how the
`reasoning_effort` vs `enable_thinking` contradiction (which caused a real HTTP 400
mid-run) was found for free.

### Step 2 — ask the endpoints directly (2 requests)

```bash
./repro/raw_curl.sh both             # one key, both endpoints, one tiny tool call
MODEL=some-new-model ./repro/raw_curl.sh both
```

The key is injected into a 0600 curl config by python — never on the command line,
where `ps` and shell history could read it.

What "healthy" looks like:

```
-- OpenAI compatible-mode / glm-5.3
   http=200 time=1.56s
  finish=tool_calls tool_calls=1 reasoning=yes
  TOOL: get_weather({"city": "Osaka"})
  VERDICT: OK tool call
-- Anthropic messages / glm-5.3
   http=200 time=1.35s
  stop_reason=tool_use blocks=['thinking', 'tool_use'] tool_use=1
  VERDICT: OK tool_use
```

If you get `http=400 Request body format invalid` from the anthropic endpoint,
**check your own request shape first**: it is Anthropic-native
(`input_schema`, `tool_choice: {"type":"auto"}`), not OpenAI-shaped
(`parameters`). That mistake was made here, and it was ours.

### Step 3 — through the SDK, then through a whole agent (2–3 requests)

```bash
$V repro/ab_probe.py --lane both              # litellm layer, both transports
$V repro/sdk_conversation.py --lane anthropic # a real Agent+Conversation turn
```

### Step 4 — only now add the lane

```yaml
# router/router.yaml, under lanes:
my_new_lane:
  model: "anthropic/some-model"     # prefix MUST match the endpoint below
  purpose: "what this lane is for"
  max_iterations: 20
  max_requests_per_run: 20
  # If you set enable_thinking: false you MUST also disable reasoning effort,
  # or the router refuses to load and tells you why.
  extra_body:
    enable_thinking: false
  capability_overrides:
    supports_reasoning_effort: false
  probes:
    raw_curl: verified_here         # honest provenance, not aspirational
    litellm: verified_here
    sdk_conversation: verified_here
    tools_required: true
```

Then point a role at it, and run `--plan` (free) before anything else.

The `probes:` block is not decoration. `lane_for()` refuses a tool-calling lane
whose `raw_curl`/`litellm` aren't at least `verified_in_brief` — a lane nobody
probed can't silently take traffic. Marking something `verified_here` that you only
read about in a brief is exactly the dishonesty that wastes quota.

### Step 5 — the free checks that catch most wiring bugs

```bash
$V team/run_team.py --plan    --brief BRIEF.md   # every role actually gets tools
$V team/run_team.py --dry-run --brief BRIEF.md --workspace /path/to/repo
$V -m pytest tests/ -q                            # config self-consistency
```

---

## `team/teamkit.py` — run records, pair acceptance, budget chokepoint (R1)

`teamkit.run_team()` is the sanctioned single-lane launcher. Three disciplines
sit around the original flow (preflight → lane check → register agents → send +
run → gate → lessons → git diff/log); none of them replace it.

### RunRecord (D1)

Every run that gets as far as taking the lock persists
`runs/<utc-%Y%m%dT%H%M%SZ>-<slug>/` (`runs/` is gitignored; `slug` = the first
24 safe characters of `basename(workspace)`; a second run in the same second
gets `-2`, so records never overwrite each other):

| File | Contents |
|---|---|
| `config.json` | resolved workspace / model / base_url / budget_calls / roles / contract **verbatim**, plus `brief_sha256` — the hash of the brief text, never the text |
| `state.json` | status + `attempts` for `preflight`, `team_run`, `gate`, `lessons`. A stage that raised is recorded with `TypeError: <message tail, last 300 chars>` and the record is still written |
| `summary.json` | exactly the dict `run_team()` returned, plus `run_dir` |
| `retained.json` | only for gate v2: `{accepted, baseline_ref, head_ref, g_new, g_base}` |

Recording is wrapped so a failure **inside** it can never crash a run or change
what it returns: the dict then simply carries `run_dir: null`. One lock per run
slug (`runs/.locks/<slug>.lock`, `O_EXCL`, containing the pid): a second runner
aiming at the same live slug is refused with `FileLockError` **before** any API
call is spent; a lock whose pid is dead is reclaimed.

### Gate v2 — component check + pair acceptance vs the incumbent (D2)

`contract={"gate": cmd}` alone behaves exactly as before. Adding
`"incumbent_gate"` makes grading a *pair* comparison, because "the tests pass"
is not the question — "does the new work beat what we already had?" is:

| Check | Where it runs | Meaning |
|---|---|---|
| `g_new` (component) | the workspace, as today (timeout 900) | the new work satisfies the gate |
| `g_base` (pair) | a **pristine baseline** materialized outside `runs/` | whether the incumbent already did |

The baseline is captured **at run start, before the team touches anything**:
`git stash create` when the workdir is dirty, otherwise the HEAD sha. It is then
materialized with `git worktree add --detach`, falling back to `git archive |
tar -x` when worktrees are refused (they are, on some dirty/untracked-only
trees). **Baseline means committed/stashed state only.** Uncommitted *tracked*
edits are in it because `stash create` carries them; untracked files are **not**
— which is why an incumbent gate that happens to need a file the team created
before the run started will fail, and that is the honest answer: the incumbent
did not have it. The user's workspace is never mutated for the pair check (no
`stash apply`, no `checkout`, no stash entry), and the temp tree is removed in a
`finally` (`git worktree remove --force` + `prune`).

| Old status (v1) | New status (v2) | Condition | Retained? |
|---|---|---|---|
| `GATE_FAIL` | `GATE_FAIL` | `g_new` fails | **no** — incumbent retained |
| `GATE_PASS` | `GATE_PASS_TIE` | `g_new` pass, `g_base` pass | yes — new merely matches baseline |
| `GATE_PASS` | `GATE_PASS_NEW` | `g_new` pass, `g_base` fail | yes — measurable improvement |

With `incumbent_gate` present the legacy string `GATE_PASS` is **never**
emitted; the return dict gains `pair={g_new, g_base, baseline_ref}` (and
`pair: null` for legacy single-gate runs). If no baseline can be established
(not a git work tree), the pair check is recorded as un-run and reported as
`GATE_PASS_TIE` with the reason in `g_base.error` — an unknown incumbent is
never advertised as a beaten incumbent.

### Budget chokepoint (D3)

Both guards run at the very top of `run_team()`, before `preflight()`, so
neither can spend anything:

* `budget_calls > 120` → `{"status": "BUDGET_REFUSED", ...}` with the existing
  keys emptied. No run dir, no lock, no network.
* `TEAMKIT_DRY_RUN=1` → writes `config.json` + `state.json` into a real run dir,
  prints the plan (workspace, model, `base_url` **after** `lane_check`, stages,
  both gate commands) to stdout, returns `{"status": "DRY_PLAN", "run_dir": ...}`
  with zero API calls.

`team/run_team.py`'s own `--plan` / `--dry-run` flags are a different code path
and are untouched.

---

## The four rules this repo enforces in code

| Rule | Where it's enforced |
|---|---|
| A sub-agent that can't act must fail *before* it's used | `fix/oh_compat.assert_runnable`, called from `Team.build_agent` and `Team()` |
| Prefix and endpoint must agree | `fix/oh_compat.validate_lane`, at config-parse time |
| `enable_thinking: false` + reasoning effort = contradiction | `router.Router._check_thinking_consistency` refuses to load |
| Requests are the budget unit, and only real spend is recorded | `lib/ledger.py` + `Team._charge` + `tests/test_ledger.py` |
| Secrets never reach a file, a log, a commit, or stdout | `lib/keys.redact` on every write + `lib/secret_scan.py` as the gate + `.gitignore` on `runs/` |
| Model-authored "acceptance commands" are never shell-executed by the orchestrator | `team/run_team.py` — they are recorded and reported, and a human/reviewer runs them |

The last one is worth pausing on: it means `Team` never *verifies* gates itself.
That is deliberate. Auto-running model-authored strings is how a "review" step
turns into arbitrary code execution, and it's the difference between a tool that
checks work and a tool that does whatever the checked party tells it to.

---

## Honest limitations

- **Sub-agent delegation is broken.** `docs/DELEGATION-INCIDENT.md`. The `task`
  tool hands out only `finish`/`think`. Tested against the frontmatter cause and
  refuted — it's a separate, unresolved tool-registration defect. So "multi-agent"
  here means one orchestrator driving several role-scoped conversations in
  parallel where files are disjoint. That works (see `docs/LIVE-RUN.md`); it is not
  what the brief pictured, and it is not faked to look like it.
- **Cost tracking is in requests, not dollars.** litellm has no price map for
  these model ids, so `accumulated_cost` is always $0.00 and the SDK's USD budget
  guard can never trip. We count requests instead — which is the unit the plan
  actually bills.
- **The empty-turn loop is stochastic.** A tool-less agent sometimes emits nothing
  and sometimes escapes with prose. Both were observed. The deterministic harm is a
  crippled agent, and that's what the guard checks.
- **`max_budget_per_run` and `prompt_cache_key` need `LocalConversation`.** The
  public `Conversation` factory raises `TypeError`. Use
  `router.public_conversation_kwargs()` when you use the factory.

## Reading order for a human

1. `repro/FINDINGS.md` — what was wrong, and the six hypotheses that were wrong first
2. `docs/LIVE-RUN.md` — proof the fixed thing actually works, with real numbers
3. `router/router.yaml` — the policy, in one file
4. `team/HANDOFF.md` — how to use it and how to audit it
5. `docs/DELEGATION-INCIDENT.md` — what still doesn't work
