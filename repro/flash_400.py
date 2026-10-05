"""Why does the fixer lane get HTTP 400? Prints the SDK's own error event.

    python3 repro/flash_400.py            # 1-2 calls

The live smoke run failed with `POST .../compatible-mode/v1/chat/completions
400 Bad Request` on openai/qwen3.8-flash, while the identical model answered a
plain completion fine (see below). This isolates which part of the SDK request --
tools, schemas, the security-risk field, or extra_body -- the endpoint rejects.

Arms are ordered cheapest-first and stop at the first failure, because the whole
point is to find the ONE thing that breaks it, not to pay for all of them.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from capture import capture, now_iso, record_calls  # noqa: E402
from fix.oh_compat import ensure_tools_registered  # noqa: E402
from lib.keys import COMPATIBLE_BASE_URL, token_plan_key  # noqa: E402

MODEL = os.environ.get("FLASH_MODEL", "openai/qwen3.8-flash")


def _conversation_state():
    """A real ConversationState, without making an API call.

    ConversationState(agent=None) blows up inside pydantic (the agent field is a
    discriminated union and None is not a valid subtype), and constructing an LLM
    is required to build an Agent. So build the whole stack against an unroutable
    base_url: no request can leave the box, and constructing a Conversation costs
    nothing. The terminal-session destructor noise at interpreter shutdown is
    cosmetic.
    """
    import tempfile

    from openhands.sdk import Agent, Conversation, LLM
    from openhands.tools.preset.default import get_default_tools

    llm = LLM(model=MODEL, api_key="x" * 20, base_url="https://invalid.invalid/v1")
    specs = list(get_default_tools(enable_browser=False, enable_sub_agents=False))
    agent = Agent(llm=llm, tools=specs)
    conv = Conversation(agent=agent, workspace=tempfile.mkdtemp())
    return conv, specs


def sdk_tools(with_security: bool):
    """Real ToolDefinitions, built exactly the way Agent does at runtime."""
    from openhands.sdk.tool.registry import resolve_tool

    conv, specs = _conversation_state()
    defs: list = []
    for spec in specs:
        defs.extend(resolve_tool(spec, conv.state))
    tools = [d.to_openai_tool(add_security_risk_prediction=with_security)
             for d in defs]
    return tools, [d.name for d in defs]


def probe(name: str, *, tools: list | None, security: bool, extra_body: dict | None
          ) -> dict:
    import litellm

    kwargs: dict = {
        "model": MODEL,
        "api_key": token_plan_key(),
        "api_base": COMPATIBLE_BASE_URL,
        "messages": [{"role": "user", "content": "List the files in this directory."}],
        "max_tokens": 256,
        "temperature": 0.0,
    }
    if tools is not None:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if extra_body:
        kwargs["extra_body"] = extra_body
    out: dict = {"probe": name, "security_risk": security, "ts": now_iso(),
                 "n_tools": len(tools) if tools is not None else None,
                 "tool_names": [t["function"]["name"] for t in tools] if tools else None,
                 "kwargs_keys": sorted(kwargs)}
    try:
        resp = litellm.completion(**kwargs)
        msg = resp.choices[0].message
        out.update({"ok": True, "finish": resp.choices[0].finish_reason,
                    "tool_calls": len(msg.tool_calls or []),
                    "text_len": len(msg.content or "")})
    except Exception as exc:
        body = ""
        try:
            body = str(getattr(exc, "body", ""))[:400]
        except Exception:
            pass
        out.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:400]}",
                    "body": body, "status": getattr(exc, "status_code", None)})
    finally:
        record_calls(MODEL, 1)
    return out


def main() -> int:
    ensure_tools_registered()
    results: list[dict] = []

    # A: plain completion, no tools at all. Known-good baseline.
    results.append(probe("A_no_tools", tools=None, security=False, extra_body=None))
    if not results[-1]["ok"]:
        return report(results)

    # B: SDK-built tools, no security_risk field.
    tools_plain, names = sdk_tools(False)
    results.append(probe("B_sdk_tools", tools=tools_plain, security=False,
                         extra_body=None))
    if not results[-1]["ok"]:
        return report(results)

    # C: SDK tools WITH the extra security_risk field the Agent path adds.
    tools_risk, _ = sdk_tools(True)
    results.append(probe("C_security_risk", tools=tools_risk, security=True,
                         extra_body=None))

    # D: plus the router's extra_body toggle.
    results.append(probe("D_extra_body", tools=tools_risk, security=True,
                         extra_body={"enable_thinking": False}))

    # E: the conversation object that actually failed -- what differs?
    results.append(describe_failed_conversation(names))
    return report(results)


def describe_failed_conversation(tool_names: list[str]) -> dict:
    """Run the real Agent path once and capture the SDK's own error event."""
    from openhands.sdk import Agent, Conversation, LLM

    llm = LLM(model=MODEL, api_key=token_plan_key(), base_url=COMPATIBLE_BASE_URL,
              streaming=False, temperature=0.0, litellm_extra_body={"enable_thinking": False},
              retry_max_attempts=1)
    from openhands.tools.preset.default import get_default_tools

    agent = Agent(llm=llm, tools=list(get_default_tools(enable_browser=False,
                                                        enable_sub_agents=False)))
    errors: list[str] = []

    def on_event(event) -> None:
        if "Error" in type(event).__name__:
            errors.append(f"{type(event).__name__}: "
                          f"{getattr(event, 'llm_error', None) or getattr(event, 'error', '')}"
                          [:500])

    workspace = Path(os.path.join(os.environ.get("TMPDIR", "/tmp"), "flash400_ws"))
    workspace.mkdir(parents=True, exist_ok=True)
    conv = Conversation(agent=agent, workspace=str(workspace),
                        callbacks=[on_event], max_iteration_per_run=1)
    conv.send_message("List the files here using the terminal tool.")
    try:
        conv.run()
    except Exception as exc:
        errors.append(f"run raised {type(exc).__name__}: {str(exc)[:300]}")
    record_calls(MODEL, 1)
    return {"probe": "E_real_agent", "tool_names": tool_names,
            "sdk_errors": errors or ["(none captured)"],
            "agent_turns": len([e for e in errors if e])}


def report(results: list[dict]) -> int:
    path = capture("flash_400", {"model": MODEL, "results": results})
    print(f"\n--- capture: {path.relative_to(REPO)} ---")
    for r in results:
        print(f"[{r['probe']:18s}]", end=" ")
        if "sdk_errors" in r:
            print(f"real-agent errors -> {r['sdk_errors']}")
            continue
        if r.get("ok"):
            print(f"OK finish={r['finish']} tool_calls={r['tool_calls']} "
                  f"text_len={r['text_len']} n_tools={r['n_tools']}")
        else:
            print(f"FAIL status={r.get('status')} {r['error'][:220]}")
            if r.get("body"):
                print(f"       body: {r['body'][:300]}")
    failing = [r["probe"] for r in results if not r.get("ok")
               and "sdk_errors" not in r]
    if failing:
        print(f"\nVERDICT: first failing arm = {failing[0]}; the delta between it "
              "and the previous arm is the cause")
    else:
        print("\nVERDICT: every arm passed -- the 400 came from something this "
              "probe does not reproduce (look at message content, not tools)")
    print(f"COST: {len([r for r in results if 'sdk_errors' not in r])} call(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
