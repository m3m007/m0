"""Reproduce the failure IN the SDK on anthropic/glm-5.3 -- 2 API calls.

    python3 repro/empty_turn_demo.py

Deliverable #1(a) of the brief asks for a script that makes the failure
reproduce through the SDK with ``anthropic/glm-5.3``. This is it. The honest
caveat, stated by the script itself in its verdict: the trigger is the
sub-agent's **empty tool set** (RC1), not the transport and not thinking-model
handling. Same model, same endpoint, same prompt -- the only variable is tools.

A/B design (both turns use anthropic/glm-5.3):
    ARM 1  Agent(llm, tools=[terminal, file_editor])  -> acts, no nudge
    ARM 2  Agent(llm, tools=[])                       -> the netflow condition:
             no tool call, no content, corrective feedback injected

ARM 2 is what every sub-agent in the netflow run actually was, because their
role files' frontmatter was not on line 1 (repro/subagent_tools.py, 0 calls).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from capture import capture, now_iso, record_calls  # noqa: E402
from fix.oh_compat import ALWAYS_ON, build_llm, ensure_tools_registered  # noqa: E402

TASK = "Run `echo demo-ok` with the terminal tool, then call finish."
NUDGE_MARK = "did not include a"


def observe(events: list, nudges: list):
    """Return an on_event callback that records agent turns and framework nudges."""
    from openhands.sdk.event.llm_completion_log import LLMCompletionLogEvent  # noqa: F401

    def on_event(event) -> None:
        name = type(event).__name__
        if name == "MessageEvent":
            msg = getattr(event, "llm_message", None)
            if msg is None:
                return
            text = "".join(getattr(p, "text", "") or "" for p in (msg.content or []))
            events.append({
                "source": getattr(event, "source", "?"),
                "n_tool_calls": len(msg.tool_calls or []),
                "tool_names": [getattr(c, "name", "?") for c in (msg.tool_calls or [])],
                "text_len": len(text),
                "reasoning_len": len(msg.reasoning_content or ""),
                "n_thinking_blocks": len(msg.thinking_blocks or []),
            })
            if getattr(event, "source", "") == "environment" and NUDGE_MARK in text:
                nudges.append(text[:60])
        elif name == "ActionEvent":
            events.append({"action": type(getattr(event, "action", None)).__name__})

    return on_event


def run_arm(name: str, model: str, tools: list, max_iter: int) -> dict:
    from openhands.sdk import Agent, Conversation

    registered = ensure_tools_registered()
    workspace = Path(tempfile.mkdtemp(prefix=f"emptyturn_{name}_"))
    events: list = []
    nudges: list = []

    llm = build_llm(model)
    specs = []
    if tools:
        from openhands.tools.preset.default import get_default_tools

        available = {t.name: t for t in get_default_tools(
            enable_browser=False, enable_sub_agents=False)}
        specs = [available[t] for t in tools if t in available]

    agent = Agent(llm=llm, tools=specs,
                  system_prompt="You are a single-purpose agent. Use a tool to act; "
                                "do not reply with prose only.")
    conv = Conversation(agent=agent, workspace=str(workspace),
                        callbacks=[observe(events, nudges)],
                        max_iteration_per_run=max_iter)
    out: dict = {"arm": name, "model": model, "requested_tools": tools,
                 "registered_tools": registered, "ts": now_iso(), "error": None}
    try:
        conv.send_message(TASK)
        conv.run()
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"

    agent_turns = [e for e in events if e.get("source") == "agent"]
    empty = [e for e in agent_turns if not e["n_tool_calls"] and not e["text_len"]]
    action_names = [e["action"] for e in events if "action" in e]
    # Authoritative request count: the SDK records one usage entry per completion.
    try:
        completions = len(llm.metrics.token_usages)
    except Exception:
        completions = max(len(agent_turns), 1)
    out.update({
        "agent_tools": sorted({getattr(t, "name", str(t)) for t in agent.tools}),
        "n_agent_turns": len(agent_turns),
        "n_empty_agent_turns": len(empty),
        "n_corrective_nudges": len(nudges),
        "actions": action_names,
        "events": events,
        "nudges": nudges,
        "api_calls": completions,
        "accumulated_cost": float(getattr(llm.metrics, "accumulated_cost", 0.0) or 0.0),
        # the deterministic harm: without its declared tools the agent CANNOT act
        "performed_terminal": "TerminalAction" in action_names,
    })
    shutil.rmtree(workspace, ignore_errors=True)
    record_calls(model, max(completions, 1))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="anthropic/glm-5.3",
                    help="same model for both arms; the only variable is tools")
    ap.add_argument("--max-iter", type=int, default=2)
    args = ap.parse_args(argv)

    with_tools = run_arm("with_tools", args.model, ["terminal"], args.max_iter)
    without = run_arm("no_tools", args.model, [], args.max_iter)

    path = capture("empty_turn_demo", {"model": args.model,
                                       "arms": [with_tools, without]})
    print(f"\n--- capture: {path.relative_to(REPO)} ---")
    for arm in (with_tools, without):
        print(f"[{arm['arm']:11s}] {arm['model']}  requested={arm['requested_tools']}")
        print(f"            agent tools     : {arm['agent_tools']}")
        print(f"            completions     : {arm['api_calls']} "
              f"(agent turns: {arm['n_agent_turns']}, empty: {arm['n_empty_agent_turns']})")
        print(f"            corrective feed : {arm['n_corrective_nudges']}")
        print(f"            actions         : {arm['actions']}")
        print(f"            did the work    : {arm['performed_terminal']}")
        if arm["error"]:
            print(f"            error           : {arm['error'][:150]}")

    # Deterministic assertion: the harm of RC1 is that the agent CANNOT ACT,
    # whether or not this particular sample also produced an empty turn.
    blocked = (not without["performed_terminal"]) and with_tools["performed_terminal"]
    looped = without["n_empty_agent_turns"] > 0 or without["n_corrective_nudges"] > 0
    print()
    if blocked and looped:
        print(f"REPRO (full loop) on {args.model}: the tool-less agent produced no "
              "tool call and no content and received corrective feedback, while the "
              "same model with the terminal tool acted immediately.")
    elif blocked:
        print(f"REPRO (degraded) on {args.model}: the tool-less agent could not "
              "perform the task at all, while the same model with tools did. This "
              "run it recovered with prose instead of an empty turn -- the empty-turn "
              "loop itself is STOCHASTIC (see FINDINGS.md); the deterministic harm is "
              "a silently crippled agent.")
    else:
        print("VERDICT: not reproduced this run -- re-run before concluding")
    print("NOTE: trigger is the empty tool set (RC1), NOT the anthropic transport "
          "and NOT thinking-model handling.")
    spent = with_tools["api_calls"] + without["api_calls"]
    print(f"COST: {spent} API call(s) recorded to the tokenwatch ledger")
    return 0 if blocked else 1


if __name__ == "__main__":
    raise SystemExit(main())
