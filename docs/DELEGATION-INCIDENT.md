# Delegation incident — read this before trusting any "team" claim in this repo

**Date:** 2026-10-05 · **Found:** at the very start of executing this brief,
before a single deliverable was written.

## What happened

The brief requires a full team workflow: `architect` plans → `fixer`s implement
in parallel → `architect` reviews adversarially. Three delegations were issued
via the `task` tool (`subagent_type` = `architect`, `fixer`, plus a bare
capability probe). All three reported the same thing:

> Available tools in this session: `finish`, `think`. I have no terminal, no
> file-editor, no file-reader. — `NO_EXEC_TOOLS`

The probe carried **no task at all** — just "enumerate your tools" — to remove
any suggestion that the agents were choosing not to act. Identical result.

## Why this is a misconfiguration, not a quirk

The role definitions declare execution tools explicitly:

```
/home/hermes/.hermes/cache/scratch/oh_team/agents/fixer.md
---
name: fixer
tools:
  - terminal
  - file_editor
permission_mode: never_confirm
---
```

Declared ≠ delivered. **The `tools:` frontmatter is not reaching the spawned
agent**, so sub-agents arrive structurally unable to complete any deliverable in
the brief.

## Was it RC1? Tested and refuted

Because `['finish', 'think']` is exactly the symptom of RC1
(`repro/FINDINGS.md`), the obvious hypothesis was: fix the role files, get tools
back. That was tested directly.

Created `~/.openhands/agents/{architect,fixer}.md` — a path the SDK really does
scan — with frontmatter on **line 1** and `tools: [terminal, file_editor,
task_tracker]`. Verified with the SDK's own loader:

```
architect: tools=['terminal', 'file_editor', 'task_tracker'] model=inherit
fixer:     tools=['terminal', 'file_editor', 'task_tracker'] model=inherit
```

The files parsed correctly. Then a sub-agent was spawned again. Still
`NO_EXEC_TOOLS`.

**Conclusion: RC1 is not the cause here.** The orchestrator's `task` tool does
not source roles from the SDK's agent directories, so this is a *separate*
tool-registration defect in the delegation channel — same *class* of bug as the
brief's subject, different component. The role files were reverted;
`~/.openhands/agents` does not exist again, and nothing about the owner's
runtime configuration was left changed.

| | Brief's bug (netflow) | This bug (orchestrator) |
|---|---|---|
| Capability exists? | endpoint returns `tool_use` fine | role files declare `terminal`/`file_editor` and *parse* with them |
| Where it dies | sub-agent tool wiring (`schema.py:338`) | delegation tool-registration layer |
| Symptom | "no tool call and no content" loop | agent turn yields no actions, only a text report |
| Root cause | RC1 — frontmatter not on line 1 | **unresolved** — not RC1 |

## Safety decisions taken

Both affected agents refused to write files blind. That refusal was correct and
is recorded here as the reason their deliverables were produced elsewhere: had
they invented `model_features.py` line numbers or log excerpts without reading
the files, fabricated "evidence" would have propagated into `FINDINGS.md` and
the fixer lanes as fact. The architect sub-agent explicitly refused on those
grounds.

## Consequence — stated plainly

The brief asks for a **multi-agent** stack. **The multi-agent portion could not
be demonstrated live in this session.**

- `repro/`, `fix/`, `router/` are real, tested, and their claims are backed by
  captured output.
- `team/` is written, importable, and unit-tested offline — but its `map_agents`
  parallelism has **not** been exercised against a working delegation channel.
- No deliverable here is marked "done by a sub-agent" without a gate proving it.

## What to do next

1. Inspect the orchestrator's `task` tool implementation for where the role's
   declared `tools:` list is dropped. It is a tool-registration gap, not a
   model or endpoint problem.
2. Re-run this brief once that is fixed, to validate `team/` under real
   delegation.
3. Keep `assert_runnable()` (`fix/oh_compat.py`) in every spawn path: whatever
   the cause, an agent that cannot act should fail at construction, not after 12
   corrective-feedback rounds.
