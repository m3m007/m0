# FINDINGS. "No tool call and no content" in the OpenHands team run

Date: 2026-10-05 · Decisive experiment cost: **0 API calls.**

## Verdict

Not thinking-model handling, not `model_features.py`, not the transport. Endpoints return proper tool calls; a full agent turn completes real tasks with 93% cache hit. The defect: **sub-agents spawned with zero executable tools** (only `finish`/`think`). Each turn produced neither tool call nor text, corrective feedback looped until the stuck-detector killed it. Three usage errors on our side, and none of them raise.

## Root causes

**RC1 (primary): role frontmatter not on line 1.** Files open with a title line; python-frontmatter only parses when line 1 is exactly `---`:

```markdown
# architect        <- this line kills parsing
---
name: architect
tools: [terminal, file_editor]
---
```

Chain: `_extract_tools({})` → `[]` → registry builds empty list → `Loaded 0 tools from spec` → built-ins only (`FinishTool`, `ThinkTool`) → `Tool 'terminal' not found. Available: ['finish', 'think']`. Nothing raises; failure surfaces later as wasted quota. (`schema.py:338`, `registry.py:243-250`, `base.py:596`, `agent.py:1349`)

**RC2: `model:` in a role file is a profile name, not a model id.** Non-`inherit` values key into `LLMProfileStore`; `~/.openhands/profiles` doesn't exist here → `ValueError` at spawn. RC1 masked RC2 (ignored metadata fell back to `inherit`). Fix both together or you trade a silent loop for a hard crash.

**RC3: the tool registry populates lazily.** Validating declared tools before `get_default_tools()` is imported raises `Tool 'terminal' not registered`. Load the preset first, always.

## Evidence

From `oh-netflow-team.log`: `Loaded 0 tools from spec` ×9, `LLM response contained no tool call and no content` ×12, `Tool 'X' not found` ×6 (`terminal`×2, `file_editor`×1, `run_bash`×2, `tool`×1). Warning path: `response_dispatch.py:364-371` `_send_corrective_nudge` via `classify_response` (returns EMPTY only when no tool_calls AND no text AND no reasoning blocks). `['finish', 'think']` is RC1's fingerprint.

## Refuted hypotheses

| # | Hypothesis | Test | Result |
|---|---|---|---|
| H1 | curated `model_features.py` lists misconfigure thinking into empty turns | `show_kwargs.py` (0 calls) | REFUTED: `thinking_mode=none` sends no thinking kwargs, nothing to misconfigure; both lanes emit identical 10-key sets |
| H2 | anthropic transport broken for tools | `raw_curl.sh` (1 call) | REFUTED: clean `stop_reason=tool_use` |
| H3 | litellm mis-parses responses | `ab_probe.py` (2 calls) | REFUTED: both transports fine |
| H4 | SDK layer above litellm | `sdk_conversation.py` (1 call) | REFUTED: full turn works |
| H5 | `send_reasoning_content` gap | code read + test | Real gap, irrelevant cause: substring-match quirk never hits our ids; tool calling unaffected |
| H6 | missing `max_output_tokens` | kwargs + curl | REFUTED: succeeds with and without |
| H7 | RC1/RC2/RC3 tool wiring | `subagent_tools.py` (**0 calls**) | CONFIRMED: all role files parse `tools=[]`; same bytes with `---` on line 1 parse correctly |

Grep lesson: the logger truncates the warning to `LLM response contained no`; search fragments, not sentences.

## Correct `LLM(...)` shape

Via `fix/oh_compat.correct_llm_kwargs()`: prefix must match base_url, `streaming=False`, `drop_params=True`, retries set, key resolved at runtime. Pinned traps:

- `max_iteration_per_run` / `max_budget_per_run` are NOT `LLM` fields (pydantic `extra="ignore"` silently drops them). Iterations go on `Conversation`; budget + `prompt_cache_key` only on `LocalConversation`.
- Mismatched prefix/base_url → opaque HTTP 400; `validate_lane()` rejects pre-spend.
- `capability_overrides` exists (`llm.py:459`) but does not expose `send_reasoning_content` (curated-list only).

## Still live

The orchestrator's own `task` tool reports `['finish', 'think']`, the same fingerprint; delegation channel affected (`docs/DELEGATION-INCIDENT.md`). Likely fix: role files in a scanned dir (`~/.openhands/agents` etc., none exist here) with frontmatter on line 1 + RC3 import order. Untested, deliberately not applied mid-session; tracked as unfinished work.

## Reproduce

```bash
V=/home/hermes/.hermes/cache/scratch/oh-venv/bin/python
$V repro/subagent_tools.py            # 0 calls: root cause, deterministic
$V repro/show_kwargs.py --json        # 0 calls: real outbound kwargs
./repro/raw_curl.sh both              # 2 calls: endpoints healthy
$V repro/ab_probe.py --lane both      # 2 calls: litellm layer
$V repro/sdk_conversation.py --lane anthropic   # 1 call: full turn works
$V -m pytest tests/ -q                # 0 calls: all offline
```

`runs/` gitignored; captures pass `lib.keys.redact()`; `lib/secret_scan.py` blocks credential-shaped commits.
