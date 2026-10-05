# FINDINGS — why the OpenHands team run produced "no tool call and no content"

Date: 2026-10-05 · Investigated in: `/home/hermes/projects/multiagent-stack`
Cost of the investigation's decisive experiment: **0 API calls.**

---

## 1. Verdict in one paragraph

The failure was **not** thinking-model handling, **not** `model_features.py`, and
**not** the `anthropic/` transport. Both Token Plan endpoints return proper tool
calls for `glm-5.3`, and a full OpenHands agent turn on `anthropic/glm-5.3`
completes a real multi-step task with a 93% cache hit. The real defect is that
**our sub-agents were spawned with zero executable tools** — they could only see
`finish` and `think`. Each turn therefore produced neither a usable tool call nor
text, the framework injected corrective feedback, and the loop repeated until the
stuck-detector killed the task. Three separate usage errors on our side caused
it, and the SDK's sin is that **none of them raise**.

## 2. Root causes

### RC1 — role Markdown frontmatter was not on line 1 (primary cause)

Our role files open with a title line:

```markdown
# architect
---
name: architect
tools:
  - terminal
  - file_editor
---
```

`openhands/sdk/subagent/schema.py:338` does `frontmatter.load(f)`, and
python-frontmatter only recognises frontmatter when the **first line** is exactly
`---`. With a heading in the way, `post.metadata` is `{}`. So:

- `schema.py:57 _extract_tools({})` → `[]`
- `subagent/registry.py:243-250` builds an empty tool list
- `agent/base.py:596` logs `Loaded 0 tools from spec`
- the agent gets only the built-ins (`agent/base.py:163`: `include_default_tools`
  defaults to `FinishTool`, `ThinkTool`)
- `agent/agent.py:1349-1352` then logs `Tool 'terminal' not found. Available:
  ['finish', 'think']`

**Nothing raises.** The file is valid Markdown, the definition loads, the agent
constructs. The failure only shows up as wasted quota minutes later.

### RC2 — `model:` in a role file is a PROFILE NAME, not a model id

`subagent/registry.py:216-226` treats a non-`inherit` `model:` as a key into an
`LLMProfileStore` and raises `ValueError: Profile ... not found in profile store`
if absent. `~/.openhands/profiles` does not exist on this box, so the store is
empty. `oh_team2/architect.md` sets `model: anthropic/deepseek-v4-pro`.

**Interaction that matters:** RC1 *masked* RC2. Because metadata was ignored,
`model` fell back to the default `"inherit"` (schema.py:345), so the profile
lookup never ran. Fix RC1 alone and the run trades a silent loop for a hard
crash at spawn. Both must be fixed together.

### RC3 — the tool registry is populated lazily

`agent_definition_to_factory` validates declared tools against
`list_registered_tools()`. That registry is empty (`['ClassifyAndSwitchLLMTool']`)
until `openhands.tools.preset.default.get_default_tools()` is imported/called.
Registering a sub-agent before that import raises `Tool 'terminal' not registered`.
Every team runner must load the preset first.

## 3. Evidence chain, verbatim

From `~/.hermes/cache/scratch/oh-netflow-team.log` (log wraps its own columns;
read it as printed):

```
[10/05/26 01:26:26] INFO     Loaded 0 tools from spec                base.py:596
[10/05/26 01:26:31] ERROR    Tool 'terminal' not found. Available: agent.py:1352
                             ['finish', 'think']
[10/05/26 01:26:31] ERROR    Tool 'terminal' not found. Available: agent.py:1352
                             ['finish', 'think']
[10/05/26 01:27:44] WARNING  LLM response contained no  response_dispatch.py:370
                             tool call and no content -
                             sending corrective
                             feedback
[10/05/26 01:29:05] WARNING  LLM response contained no  response_dispatch.py:370
[10/05/26 01:30:26] WARNING  LLM response contained no  response_dispatch.py:370
[10/05/26 01:30:26] WARNING  Stuck pattern detected.   local_conversation.py:761
[10/05/26 01:30:26] WARNING  Task 'task_00000001' stopped: status manager.py:425
                             'stuck'.
```

Counts in that log: `Loaded 0 tools from spec` ×9, `LLM response contained no
tool call and no content` ×12, `Tool 'X' not found` ×6
(`terminal` ×2, `file_editor` ×1, `run_bash` ×2, `tool` ×1).

The warning text comes from `agent/response_dispatch.py:364-371`
(`_send_corrective_nudge`), reached via
`_handle_no_content_response` (response_dispatch.py:272-301), whose classifier is
`classify_response` (response_dispatch.py:54-80):

```python
    if message.tool_calls:
        return LLMResponseType.TOOL_CALLS
    if any(isinstance(c, TextContent) and c.text.strip() for c in message.content):
        return LLMResponseType.CONTENT
    if (message.responses_reasoning_item is not None
            or message.reasoning_content is not None
            or message.thinking_blocks):
        return LLMResponseType.REASONING_ONLY
    return LLMResponseType.EMPTY
```

Note the log line "Available: `['finish', 'think']`" — that is RC1's fingerprint,
and it is exactly what the live `task` tool reports today (see §6).

## 4. Hypotheses tested and killed (the honest list)

| # | Hypothesis | Source | Test | Result |
|---|---|---|---|---|
| H1 | **`model_features.py` curated lists lack our model ids → thinking misconfigured → empty turns** | the brief's own lead | `repro/show_kwargs.py` (0 calls): dump `get_features()` + outbound kwargs | **REFUTED.** `anthropic/glm-5.3` → `thinking_mode=none`, `supports_extended_thinking=False`, `send_reasoning_content=False`. `thinking_mode=none` means the SDK sends **no** thinking kwargs at all — there is nothing to misconfigure. Both lanes emit the identical 10-key kwarg set. |
| H2 | The `anthropic/` transport/endpoint is broken for tool calls | brief's framing | `repro/raw_curl.sh anthropic` (1 call) | **REFUTED.** `stop_reason=tool_use`, `blocks=['thinking','tool_use']`, `get_weather({"city": "Osaka"})`. |
| H3 | LiteLLM mis-parses anthropic-messages tool responses | implied by H2 | `repro/ab_probe.py --lane both` (2 calls) | **REFUTED.** Both transports: `finish=tool_calls`, 1 tool call, valid JSON args. |
| H4 | Failure is in the SDK layer above litellm (message formatting / response parsing) | — | `repro/sdk_conversation.py --lane anthropic` (1 call) | **REFUTED.** Real `Agent`+`Conversation` on `anthropic/glm-5.3` ran `TerminalAction` then `FinishAction`, wrote the file, task verified. |
| H5 | `send_reasoning_content` gap causes it | brief's hint | `model_features.py:294` + `get_features` | **Real gap, irrelevant cause.** The list entry `deepseek/deepseek-v4-pro` never matches our id `anthropic/deepseek-v4-pro` because `model_matches` is case-insensitive *substring* matching (model_features.py:280 note) against a differently-prefixed id, and `capability_overrides` does not expose `send_reasoning_content` (verified in `tests/test_oh_compat.py`). Tool calling is unaffected. |
| H6 | Missing `max_output_tokens` truncation (endpoint requires it) | — | kwargs dump + raw curl | **REFUTED as cause.** We send no `max_tokens` via the SDK and still get tool calls; raw curls that *did* set `max_tokens` also succeeded. |
| H7 | **RC1/RC2/RC3 — tool wiring** | log grep | `repro/subagent_tools.py` (**0 calls**) | **CONFIRMED.** All four on-disk role files parse to `tools=[]`. Byte-identical file with `---` on line 1 parses to `['terminal','file_editor']`. |

Also corrected along the way: the brief cites `oh-netflow-team.log` for
timestamps 01:27/01:34/01:54 — that file does contain them (§3). My first grep
missed it because I searched the full sentence while the logger **truncates** the
message to `LLM response contained no`. A reminder to grep on fragments, not
assumptions.

## 5. What `LLM(...)` should look like

`fix/oh_compat.correct_llm_kwargs()` produces it; see `fix/README.md` for the
rationale and `tests/test_oh_compat.py` for the proofs.

```python
from openhands.sdk import LLM
from lib.keys import ANTHROPIC_BASE_URL, token_plan_key

llm = LLM(
    model="anthropic/glm-5.3",          # prefix MUST match the endpoint below
    api_key=token_plan_key(),           # resolved at runtime; never a literal
    base_url=ANTHROPIC_BASE_URL,        # .../apps/anthropic
    streaming=False,                    # removes SSE-parsing variance
    temperature=0.0,
    drop_params=True,
    retry_max_attempts=3, retry_multiplier=2.0,
    retry_min_wait=2, retry_max_wait=30,
    # OPTIONAL public override hook (llm.py:459) — the brief asked whether one
    # exists. It does, and it is the right lever if a lane ever needs thinking:
    # capability_overrides={"thinking_mode": "manual", "supports_prompt_cache": True},
)
```

Traps the tests pin down:
- **`max_iteration_per_run` / `max_budget_per_run` are not `LLM` fields.** `LLM`
  uses pydantic `extra="ignore"`, so passing them **silently does nothing**.
  `max_iteration_per_run` belongs on `Conversation`; `max_budget_per_run` and
  `prompt_cache_key` exist only on `LocalConversation` (verified against
  `LocalConversation.__init__` varnames).
- **`anthropic/x` with a `compatible-mode` base_url** (or vice versa) answers an
  opaque HTTP 400. `validate_lane()` rejects it before any spend.
- **`send_reasoning_content` is not overridable** — curated-list only (H5).

## 6. This is not merely historical

My own sub-agents in this very session reported:

> Available tools in this session: `finish`, `think` … `NO_EXEC_TOOLS`

`['finish', 'think']` is precisely RC1's fingerprint. The live `task` tool is
reading role files that suffer the same leading-title-line frontmatter problem,
so **the delegation channel of this orchestrator is affected by the same root
cause**. Details and the honest consequences are in
`docs/DELEGATION-INCIDENT.md`.

Candidate remedy (untested, and deliberately not applied): the discovered search
paths are `~/.agents/agents`, `~/.openhands/agents`, `<project>/.agents/agents`,
`<project>/.openhands/agents`; **none exist on this box**. Re-creating the role
files there with frontmatter on line 1, plus RC3's preset import, is the
likely fix. I am not editing the orchestrator's own runtime configuration
mid-session on the strength of an untested inference — that is a follow-up, and
it is listed in `DONE.md` as unfinished work rather than claimed as done.

## 7. Reproduce it yourself

```bash
V=/home/hermes/.hermes/cache/scratch/oh-venv/bin/python
$V repro/subagent_tools.py            # 0 calls  — the root cause, deterministically
$V repro/show_kwargs.py --json        # 0 calls  — the kwargs the SDK really sends
./repro/raw_curl.sh both              # 2 calls  — both endpoints, raw, healthy
$V repro/ab_probe.py --lane both      # 2 calls  — litellm layer, both transports
$V repro/sdk_conversation.py --lane anthropic   # 1 call — full agent turn works
$V -m pytest tests/ -q                # 0 calls  — 43 tests, all offline
```

`runs/` is gitignored; every capture passes through `lib.keys.redact()` on the
way to disk, and `lib/secret_scan.py` blocks a commit if anything credential-shaped
appears anywhere in the tree.
