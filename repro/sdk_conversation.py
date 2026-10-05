"""Highest-fidelity repro: a real ``Conversation`` turn, exactly as the team ran.

    python3 repro/sdk_conversation.py --lane anthropic      # 1 call, 1 iteration
    python3 repro/sdk_conversation.py --lane both

Raw litellm (``repro/ab_probe.py``) gets clean tool calls on BOTH transports, so
the endpoint is not at fault. This script reproduces the real path from
``oh_netflow_team.py``:

    LLM(...) -> Agent(llm, tools) -> Conversation(agent, workspace) -> run()

and counts, per LLM turn, whether the agent produced a tool call, text, or
nothing at all -- which is what produced the "LLM response contained no tool
call and no content - sending corrective feedback" loop in the netflow log.

``max_iteration_per_run=1`` keeps one probe at exactly one paid request.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from capture import capture, now_iso, record_calls  # noqa: E402
from lib.keys import COMPATIBLE_BASE_URL, ANTHROPIC_BASE_URL, token_plan_key  # noqa: E402

LANES = {
    "anthropic": ("anthropic/glm-5.3", ANTHROPIC_BASE_URL),
    "openai": ("openai/glm-5.3", COMPATIBLE_BASE_URL),
}

TASK = ("Create a file called hello.txt in the current directory containing "
        "exactly the text TP-REPRO-OK. Then stop.")

ORCHESTRATOR_PROMPT = (
    "You are a single-purpose agent. Use the terminal tool to do the task, "
    "then call finish. Do not explain, just act."
)


def run_lane(name: str, model: str, base_url: str, overrides: dict) -> dict:
    from openhands.sdk import Agent, Conversation, Tool
    from openhands.sdk.event.llm_completion_log import LLMCompletionLogEvent
    from openhands.tools.preset.default import get_default_tools

    workspace = REPO / "runs" / f"ws_{name}"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    # NOTE: max_iteration_per_run / max_budget_per_run are Conversation args,
    # NOT LLM args -- and LLM has model_config extra="ignore", so unknown kwargs
    # are silently dropped rather than raising. Documented in FINDINGS.md.
    llm_kwargs = dict(
        model=model,
        api_key=token_plan_key(),
        base_url=base_url,
        streaming=False,
        retry_multiplier=1.0,
        retry_min_wait=1,
        retry_max_wait=1,
        retry_max_attempts=1,
    )
    llm_kwargs.update(overrides)
    llm = LLM_factory(**llm_kwargs)

    tools = get_default_tools(enable_browser=False, enable_sub_agents=False)
    agent = Agent(llm=llm, tools=tools, system_prompt=ORCHESTRATOR_PROMPT)

    turns: list[dict] = []
    nudges: list[str] = []
    actions: list[str] = []

    def on_event(event) -> None:
        cls = type(event).__name__
        if cls == "ActionEvent":
            action = getattr(event, "action", None)
            actions.append(f"{type(action).__name__}")
        if cls == "MessageEvent":
            msg = getattr(event, "llm_message", None)
            source = getattr(event, "source", "?")
            if msg is None:
                return
            text = "".join(getattr(p, "text", "") or "" for p in (msg.content or []))
            entry = {
                "source": source,
                "n_tool_calls": len(msg.tool_calls or []),
                "tool_names": [getattr(c, "name", "?") for c in (msg.tool_calls or [])],
                "text_len": len(text),
                "reasoning_len": len(msg.reasoning_content or ""),
                "n_thinking_blocks": len(msg.thinking_blocks or []),
                "is_agent_turn": source == "agent",
            }
            turns.append(entry)
            if source == "environment" and "did not include a" in text:
                nudges.append(text[:80])

    # Finding for FINDINGS.md/router docs: the public ``Conversation`` factory
    # accepts max_iteration_per_run but NOT max_budget_per_run or
    # prompt_cache_key -- those exist only on LocalConversation. Budget + cache
    # control therefore require constructing LocalConversation directly.
    conv = Conversation(agent=agent, workspace=str(workspace),
                        callbacks=[on_event],
                        max_iteration_per_run=3)
    result: dict = {"lane": name, "model": model, "api_base": base_url,
                    "ts": now_iso(), "workspace": str(workspace),
                    "llm_config": {
                        k: getattr(llm, k, "<unset>")
                        for k in ("model", "base_url", "api_version", "streaming",
                                  "capability_overrides", "drop_params",
                                  "extended_thinking_budget")
                    },
                    "error": None}
    try:
        conv.send_message(TASK)
        conv.run()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"

    result.update({
        "turns": turns,
        "n_agent_turns": sum(1 for t in turns if t["is_agent_turn"]),
        "n_empty_agent_turns": sum(
            1 for t in turns if t["is_agent_turn"]
            and not t["n_tool_calls"] and not t["text_len"]),
        "n_corrective_nudges": len(nudges),
        "actions": actions,
        "files_created": sorted(p.name for p in workspace.iterdir()),
        "hello_ok": (workspace / "hello.txt").exists()
                    and (workspace / "hello.txt").read_text(errors="replace").strip()
                    == "TP-REPRO-OK",
    })
    # one LLM turn == one request; count what actually happened
    spent = result["n_agent_turns"] or 1
    record_calls(model, spent)
    result["api_calls_spent"] = spent
    return result


def LLM_factory(**kwargs):
    from openhands.sdk import LLM

    return LLM(**kwargs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lane", choices=["anthropic", "openai", "both"], default="anthropic")
    ap.add_argument("--thinking", choices=["auto", "none", "manual", "adaptive"],
                    default="auto")
    ap.add_argument("--send-reasoning", action="store_true")
    args = ap.parse_args(argv)

    overrides: dict = {}
    caps: dict = {}
    if args.thinking != "auto":
        caps["thinking_mode"] = args.thinking
    if args.send_reasoning:
        caps["send_reasoning_content"] = True
    if caps:
        overrides["capability_overrides"] = caps

    report = {"args": vars(args), "lanes": {}}
    for name in (["anthropic", "openai"] if args.lane == "both" else [args.lane]):
        model, base_url = LANES[name]
        report["lanes"][name] = run_lane(name, model, base_url, overrides)

    path = capture(f"conv_{args.lane}_{args.thinking}", report)
    print(f"\n--- capture: {path.relative_to(REPO)} ---")
    total = 0
    for name, res in report["lanes"].items():
        total += res.get("api_calls_spent", 0)
        print(f"[{name:9s}] {res['model']}")
        if res.get("error"):
            print(f"           ERROR {res['error'][:200]}")
        print(f"           agent_turns={res['n_agent_turns']} "
              f"empty_turns={res['n_empty_agent_turns']} "
              f"nudges={res['n_corrective_nudges']} "
              f"actions={res['actions']} task_ok={res['hello_ok']}")
        for i, t in enumerate(res["turns"]):
            print(f"           turn{i} src={t['source']:11s} tools={t['n_tool_calls']} "
                  f"text={t['text_len']} reasoning={t['reasoning_len']} "
                  f"blocks={t['n_thinking_blocks']}")

    anth = report["lanes"].get("anthropic")
    if anth:
        if anth["n_empty_agent_turns"] and not anth["hello_ok"]:
            print("REPRO: anthropic lane produced empty agent turn(s) + corrective nudges")
        elif anth["hello_ok"]:
            print("VERDICT: NO-REPRO anthropic lane completed the task")
        else:
            print("VERDICT: INCONCLUSIVE (no empty turn, but task not verified)")
    print(f"COST: {total} API call(s) spent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
