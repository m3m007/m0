# DELEGATION-INCIDENT. Before trusting any "team" claim here

**Date:** 2026-10-05 · **Status 2026-10-13: CLOSED.** Delegation works; the incident was RC1 in our own wiring, fixed in #2892. SDK v1.54 `LocalConversation` auto-registers file agents from `{project}/.agents/agents`, `{project}/.openhands/agents`, `~/.agents/agents`, `~/.openhands/agents`. Live probe (clean frontmatter): child spawned via `task` reported `CHILD_TOOLS: terminal, file_editor, finish, think` and used both exec tools. Original report kept below as history.

## What happened

Brief requires: architect plans, fixers implement in parallel, architect reviews adversarially. Three `task` delegations (architect, fixer, plus a bare capability probe carrying **no task**, to rule out refusal-to-act) all reported identically:

> Available tools in this session: `finish`, `think`. No terminal, file-editor, file-reader. `NO_EXEC_TOOLS`

Role files declare execution tools explicitly (`tools: [terminal, file_editor]`): declared ≠ delivered. Sub-agents arrive structurally unable to do anything.

## Not RC1. Tested and refuted

`['finish', 'think']` is RC1's fingerprint (`repro/FINDINGS.md`), so that hypothesis was tested directly: created `~/.openhands/agents/{architect,fixer}.md` (a path the SDK really scans) with frontmatter on line 1, verified via the SDK's own loader (parses correctly), spawned again. Still `NO_EXEC_TOOLS`.

**The orchestrator's `task` tool does not source roles from the SDK's agent directories.** Separate tool-registration defect, same bug class, different component. Role files reverted; nothing left changed in runtime config.

| | netflow bug (brief's subject) | this bug |
|---|---|---|
| Where it dies | sub-agent tool wiring (`schema.py:338`) | delegation tool-registration layer |
| Symptom | empty-response loop | no actions, only text report |
| Root cause | RC1: frontmatter not line 1 | **unresolved**: not RC1 |

## Safety note

Both affected agents refused to write files blind. Correct. Had they invented line numbers or log excerpts, fabricated evidence would have propagated into `FINDINGS.md` as fact.

## Consequence

The multi-agent portion could not be demonstrated live this session.

- `repro/`, `fix/`, `router/`: real, tested, claims backed by captured output.
- `team/`: written, importable, unit-tested offline; its parallelism has never run against a working delegation channel.
- Nothing is marked "done by a sub-agent" without a gate proving it.

## Next

1. Find where `task` drops the role's declared `tools:` list. Tool-registration gap, not model/endpoint.
2. Re-run the brief after fixing, to validate `team/` under real delegation.
3. Keep `assert_runnable()` (`fix/oh_compat.py`) in every spawn path: an agent that cannot act must fail at construction, not after 12 corrective-feedback rounds.
