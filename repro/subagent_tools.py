"""ZERO-COST repro of the netflow "empty turn" loop. Costs 0 API calls.

    python3 repro/subagent_tools.py

WHAT THE BRIEF ASKED US TO REPRODUCE
    (a) failure via SDK with anthropic/glm-5.3, (b) success via openai/glm-5.3.

WHAT WE ACTUALLY FOUND
    (a) does not reproduce. ab_probe.py and sdk_conversation.py show
    anthropic/glm-5.3 returning a valid tool call and completing a real task on
    the anthropic-messages endpoint. The transport was never the problem.

    The failure is upstream of the model: sub-agents spawn with NO executable
    tools, so they ask for a tool that does not exist, get an error back, emit
    neither text nor a tool call, and the framework loops on corrective
    feedback. Two independent causes, both OUR usage errors:

    RC1  Role Markdown whose YAML frontmatter is not on LINE 1.
         subagent/schema.py:338 calls frontmatter.load(), which only recognises
         frontmatter when the first line is exactly "---". Our role files open
         with a title line ("# architect"), so post.metadata == {} ->
         _extract_tools() (schema.py:57) returns [] -> registry.py builds an
         empty tool list -> the agent sees only finish/think. Nothing raises.

    RC2  "model:" in a role file is a PROFILE NAME, not a model id.
         registry.py:216 looks it up in an LLM profile store, so
         model: anthropic/deepseek-v4-pro raises ValueError at spawn time.

Evidence chain from oh-netflow-team.log (~line 3600):
    Loaded 0 tools from spec                    <- RC1 fired
    ERROR Tool 'terminal' not found. Available: ['finish', 'think']
    WARNING LLM response contained no tool call and no content
        (agent/response_dispatch.py:365-371)
    WARNING Stuck pattern detected.  -> task marked 'stuck'
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from capture import capture  # noqa: E402
from lib.keys import ANTHROPIC_BASE_URL, token_plan_key  # noqa: E402

ROLE_DIRS = [
    "/home/hermes/.hermes/cache/scratch/oh_team2/agents",
    "/home/hermes/.hermes/cache/scratch/oh_team/agents",
]
WANTED_TOOLS = ("terminal", "file_editor")
ALWAYS_ON = {"finish", "think"}

# Same bytes, one difference: the leading title line.
BROKEN = """# architect
---
name: architect
description: Plans work and reviews results; read-only.
model: inherit
tools:
  - terminal
  - file_editor
permission_mode: never_confirm
---

You are the ARCHITECT.
"""

GOOD = """---
name: architect
description: Plans work and reviews results; read-only.
model: inherit
tools:
  - terminal
  - file_editor
permission_mode: never_confirm
---

You are the ARCHITECT.
"""


def parse_text(text: str, fname: str = "architect.md") -> dict:
    """Load one role file through the SDK's own loader; report what it parsed."""
    from openhands.sdk.subagent import load_agents_from_dir

    tmp = Path(tempfile.mkdtemp())
    (tmp / fname).write_text(text, encoding="utf-8")
    defs = load_agents_from_dir(tmp)
    if not defs:
        return {"error": "loader returned no definitions"}
    d = defs[0]
    return {
        "name": d.name,
        "parsed_tools": list(d.tools),
        "model_field": d.model,
        "description": d.description,
        "permission_mode": d.permission_mode,
        "system_prompt_len": len(d.system_prompt or ""),
        "missing_tools": sorted(set(WANTED_TOOLS) - set(d.tools)),
    }


def ensure_tools_registered() -> list[str]:
    """Populate the SDK tool registry.

    RC3 (ordering trap): ``agent_definition_to_factory`` raises
    ``Tool 'terminal' not registered`` unless the tool modules have been
    imported first -- registration is a side effect of
    ``openhands.tools.preset.default.get_default_tools``. Load the preset,
    then return the registered names.
    """
    from openhands.sdk.tool.registry import list_registered_tools
    from openhands.tools.preset.default import get_default_tools

    if "terminal" not in list_registered_tools():
        get_default_tools(enable_browser=False, enable_sub_agents=True)
    return sorted(list_registered_tools())


def runnable_tools(tools: list[str], llm) -> dict:
    """Spawn the sub-agent exactly as registry.py does; list what it can run.

    Empty frontmatter is written as ``tools: []`` rather than omitting the key,
    because YAML coerces a missing key to ``None`` -> ``[]`` -> the literal
    string ``'None'`` in the tool list, which is a confusing artefact, not the
    defect under test.
    """
    from openhands.sdk.subagent import agent_definition_to_factory, load_agents_from_dir

    ensure_tools_registered()
    tmp = Path(tempfile.mkdtemp())
    front = ["---", "name: probe", "description: d", "model: inherit"]
    if tools:
        front.append("tools:")
        front.extend(f"  - {t}" for t in tools)
    else:
        front.append("tools: []")
    front += ["---", "body"]
    (tmp / "probe.md").write_text("\n".join(front) + "\n", encoding="utf-8")
    defn = load_agents_from_dir(tmp)[0]
    out = {"declared": list(defn.tools), "runnable": None,
           "only_always_on": None, "spawn_error": None}
    try:
        agent = agent_definition_to_factory(defn, str(tmp))(llm)
        names = sorted({getattr(t, "name", str(t)) for t in agent.tools})
        out["runnable"] = names
        out["only_always_on"] = set(names) <= ALWAYS_ON
    except Exception as exc:
        out["spawn_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return out


def rc2_probe(llm) -> dict:
    """RC2: is 'model:' a model id or a profile name?"""
    from openhands.sdk.subagent import agent_definition_to_factory, load_agents_from_dir

    tmp = Path(tempfile.mkdtemp())
    (tmp / "arch.md").write_text(
        "---\nname: arch\ndescription: d\nmodel: anthropic/deepseek-v4-pro\n"
        "tools:\n  - terminal\n---\nbody\n", encoding="utf-8")
    defn = load_agents_from_dir(tmp)[0]
    try:
        agent_definition_to_factory(defn, str(tmp))(llm)
        return {"model_field": defn.model, "result": "spawned OK (unexpected)"}
    except Exception as exc:
        return {"model_field": defn.model,
                "result": f"{type(exc).__name__}: {str(exc)[:170]}"}


def main() -> int:
    from openhands.sdk import LLM

    llm = LLM(model="anthropic/glm-5.3", api_key=token_plan_key(),
              base_url=ANTHROPIC_BASE_URL, streaming=False)

    report: dict = {"api_calls_spent": 0, "role_dirs": {}, "synthetic": {}}
    report["rc2_profile_semantics"] = rc2_probe(llm)

    for d in ROLE_DIRS:
        p = Path(d)
        if p.is_dir():
            report["role_dirs"][d] = [
                parse_text(f.read_text(encoding="utf-8"), f.name)
                for f in sorted(p.glob("*.md"))
            ]

    broken = parse_text(BROKEN)
    good = parse_text(GOOD)
    report["synthetic"]["with_leading_title_line"] = broken
    report["synthetic"]["frontmatter_on_line_1"] = good
    report["synthetic"]["agent_from_broken"] = runnable_tools(broken["parsed_tools"], llm)
    report["synthetic"]["agent_from_good"] = runnable_tools(good["parsed_tools"], llm)

    path = capture("subagent_tools", report)
    print(f"\n--- capture: {path.relative_to(REPO)} ---")

    print(f"title line before frontmatter -> tools={broken['parsed_tools']} "
          f"model={broken['model_field']!r} desc={broken['description']!r} "
          f"prompt_len={broken['system_prompt_len']}")
    print(f"frontmatter on line 1         -> tools={good['parsed_tools']} "
          f"model={good['model_field']!r} desc={good['description']!r} "
          f"prompt_len={good['system_prompt_len']}")

    ab, ag = report["synthetic"]["agent_from_broken"], report["synthetic"]["agent_from_good"]
    print(f"\nBROKEN spec -> declared={ab['declared']} runnable={ab['runnable']} "
          f"only_always_on={ab['only_always_on']}")
    print(f"GOOD   spec -> declared={ag['declared']} runnable={ag['runnable']} "
          f"only_always_on={ag['only_always_on']}")
    print(f"\nRC2 'model:' semantics -> {report['rc2_profile_semantics']['result']}")

    broken_on_disk = 0
    total_on_disk = 0
    print("\nROLE FILES ON DISK:")
    for d, defs in report["role_dirs"].items():
        print(f"  {d}")
        for x in defs:
            total_on_disk += 1
            if not x.get("parsed_tools"):
                broken_on_disk += 1
                print(f"    {x['name']:12s} tools=[]   <-- frontmatter ignored")
            else:
                print(f"    {x['name']:12s} tools={x['parsed_tools']}")

    reproduced = broken["parsed_tools"] == [] and good["parsed_tools"] and \
        ab["only_always_on"] is True
    if reproduced:
        print("\nREPRO: sub-agents from our role files get ZERO runnable tools "
              "(finish/think only) -- the precondition of the empty-turn loop")
    else:
        print("\nVERDICT: mechanism not confirmed, re-inspect")
    if total_on_disk and broken_on_disk == total_on_disk:
        print(f"AFFECTED: all {total_on_disk} on-disk role files parse with no tools")
    print("COST: 0 API calls")
    return 0 if reproduced else 1


if __name__ == "__main__":
    raise SystemExit(main())
