"""SDK-level A/B: the layer the netflow team run actually used. Costs 1 call/lane.

    python3 repro/sdk_turn.py --lane both            # 2 calls
    python3 repro/sdk_turn.py --lane anthropic       # 1 call
    python3 repro/sdk_turn.py --offline              # 0 calls, kwargs only

Why this file exists: ``repro/ab_probe.py`` drives raw ``litellm.completion`` and
BOTH transports return clean tool calls. The netflow team run nevertheless
produced turns with "no tool call and no content". Therefore the failure is in
the OpenHands SDK layer -- message formatting, tool-schema construction, or
response parsing -- not the endpoint. This script drives ``LLM.completion()``
with real registered tools, i.e. the exact path an Agent uses.

Classification mirrors ``agent/response_dispatch.py:54``:
TOOL_CALLS > CONTENT > REASONING_ONLY > EMPTY. REASONING_ONLY/EMPTY for a lane
that raw litellm handled fine is the bug, isolated to the SDK.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from capture import WEATHER_PROMPT, capture, classify, now_iso, record_calls  # noqa: E402
from lib.keys import COMPATIBLE_BASE_URL, ANTHROPIC_BASE_URL, token_plan_key  # noqa: E402

LANES = {
    "anthropic": ("anthropic/glm-5.3", ANTHROPIC_BASE_URL),
    "openai": ("openai/glm-5.3", COMPATIBLE_BASE_URL),
}

# The exact LLM(...) the owner's team runner used, minus the kimi lane.
DEFAULT_KWARGS = dict(
    streaming=False,
    retry_multiplier=1.0,
    retry_min_wait=1,
    retry_max_wait=1,
    retry_max_attempts=1,
)


def make_tools():
    """Real registered tools -> real ToolDefinitions, as Agent would use."""
    from openhands.sdk import Tool
    from openhands.tools.preset.default import get_default_tools

    defs = get_default_tools(enable_browser=False, enable_sub_agents=False)
    return [Tool(name=d.name) for d in defs], [d.name for d in defs]


def build_llm(model: str, base_url: str, **overrides):
    from openhands.sdk.llm import LLM

    kwargs = dict(DEFAULT_KWARGS)
    kwargs.update(overrides)
    return LLM(model=model, api_key=token_plan_key(), base_url=base_url, **kwargs)


def resolve_tools(conversation_like_state=None):
    """Instantiate tool params for a throwaway workspace (no execution)."""
    from openhands.sdk.context import ContextSet  # noqa: F401
    from openhands.sdk.tool import ToolContext
    from openhands.tools.preset.default import get_default_tools

    workspace = REPO / "runs" / "sdk_turn_ws"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        ctx = ToolContext(workspace=str(workspace), conversation_id="repro")
    except TypeError:  # older/newer signature
        ctx = ToolContext(workspace=str(workspace))
    defs = []
    for tool in get_default_tools(enable_browser=False, enable_sub_agents=False):
        defs.append(tool)
    return defs, ctx


def offline_kwargs(model: str, base_url: str, overrides: dict) -> dict:
    llm = build_llm(model, base_url, **overrides)
    from openhands.sdk.llm.utils.model_features import get_features

    feats = get_features(model)
    try:
        prepared = llm._prepare_transport_kwargs(
            messages=[{"role": "user", "content": "ping"}], enable_streaming=False)
    except Exception as exc:  # pragma: no cover
        prepared = {"_error": f"{type(exc).__name__}: {exc}"}
    return {
        "model": model,
        "api_base": prepared.get("api_base"),
        "kwargs_keys": sorted(prepared.keys()),
        "thinking_kwargs": sorted(k for k in prepared
                                  if any(t in k.lower() for t in
                                         ("think", "reason", "budget", "effort"))),
        "features": {
            "thinking_mode": feats.thinking_mode,
            "supports_extended_thinking": feats.supports_extended_thinking,
            "send_reasoning_content": feats.send_reasoning_content,
            "supports_prompt_cache": feats.supports_prompt_cache,
            "force_string_serializer": feats.force_string_serializer,
        },
        "prepared": {k: v for k, v in prepared.items() if k != "messages"},
    }


def live_turn(model: str, base_url: str, overrides: dict, tools_wanted: bool) -> dict:
    from openhands.sdk.llm import Message, TextContent

    llm = build_llm(model, base_url, **overrides)
    tool_defs = None
    if tools_wanted:
        try:
            from openhands.tools.preset.default import get_default_tools

            tool_defs = list(get_default_tools(enable_browser=False,
                                               enable_sub_agents=False))
        except Exception as exc:  # pragma: no cover
            return {"lane_error": f"tool load failed: {type(exc).__name__}: {exc}"}

    messages = [Message(role="user", content=[TextContent(text=WEATHER_PROMPT)])]
    out: dict = {"model": model, "api_base": base_url, "ts": now_iso(),
                 "tools_wanted": tools_wanted,
                 "tool_names": [getattr(t, "name", str(t)) for t in (tool_defs or [])],
                 "llm_repr": redacted_llm_view(llm)}
    try:
        resp = llm.completion(messages=messages, tools=tool_defs,
                              add_security_risk_prediction=tools_wanted)
    except Exception as exc:
        out.update({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "classification": "ERROR"})
        record_calls(model, 1)
        return out

    msg = resp.message
    tc = list(msg.tool_calls or [])
    text = "".join(p.text for p in msg.content if getattr(p, "text", None))
    out.update({
        "ok": True,
        "classification": classify(text, tc, msg.reasoning_content or msg.thinking_blocks),
        "n_tool_calls": len(tc),
        "tool_calls": [{"name": c.name, "arguments": c.arguments,
                        "id": getattr(c, "id", None)} for c in tc],
        "text_len": len(text),
        "text_preview": text[:200],
        "reasoning_len": len(msg.reasoning_content or ""),
        "n_thinking_blocks": len(msg.thinking_blocks or []),
        "thinking_block_types": [type(b).__name__ for b in (msg.thinking_blocks or [])],
        "stop_reason": getattr(resp.raw_response, "choices", [None])[0].stop_reason
                       if getattr(resp.raw_response, "choices", None) else None,
        "metrics": {k: v for k, v in (resp.metrics.model_dump().items()
                                      if hasattr(resp.metrics, "model_dump") else [])
                    if k in ("usage", "cost", "response_time")},
    })
    record_calls(model, 1)
    return out


def redacted_llm_view(llm) -> dict:
    """What the LLM object thinks it is configured as (secrets excluded)."""
    keys = ("model", "base_url", "api_version", "streaming", "drop_params",
            "capability_overrides", "max_output_tokens", "temperature",
            "extended_thinking_budget", "retry_max_attempts")
    view = {}
    for k in keys:
        try:
            view[k] = getattr(llm, k)
        except Exception:
            view[k] = "<unset>"
    if view.get("api_key"):
        view["api_key"] = "<REDACTED>"
    return view


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lane", choices=["anthropic", "openai", "both"], default="both")
    ap.add_argument("--offline", action="store_true", help="0 calls: dump kwargs only")
    ap.add_argument("--no-tools", action="store_true",
                    help="call completion() with tools=None")
    ap.add_argument("--thinking", choices=["auto", "off", "manual", "adaptive"],
                    default="auto", help="capability_overrides thinking_mode")
    ap.add_argument("--send-reasoning", action="store_true",
                    help="capability_overrides send_reasoning_content=True")
    args = ap.parse_args(argv)

    overrides: dict = {}
    if args.thinking != "auto":
        overrides.setdefault("capability_overrides", {}).update({"thinking_mode": args.thinking})
    if args.send_reasoning:
        overrides.setdefault("capability_overrides", {})["send_reasoning_content"] = True

    wanted = ["anthropic", "openai"] if args.lane == "both" else [args.lane]
    report = {"args": vars(args), "overrides": overrides, "lanes": {}}

    for name in wanted:
        model, base_url = LANES[name]
        if args.offline:
            report["lanes"][name] = offline_kwargs(model, base_url, overrides)
        else:
            report["lanes"][name] = live_turn(model, base_url, overrides,
                                              not args.no_tools)

    path = capture(f"sdk_{'offline' if args.offline else 'live'}_{args.lane}"
                   f"_{args.thinking}{'_notools' if args.no_tools else ''}", report)
    print(f"\n--- capture: {path.relative_to(REPO)} ---")

    for name, res in report["lanes"].items():
        model = LANES[name][0]
        print(f"[{name:9s}] {model}")
        if args.offline:
            print(f"           kwargs      : {res['kwargs_keys']}")
            print(f"           thinking kw : {res['thinking_kwargs'] or '(none)'}")
            print(f"           features    : {json.dumps(res['features'])}")
        else:
            if "lane_error" in res:
                print(f"           {res['lane_error']}")
                continue
            print(f"           class       : {res.get('classification')} "
                  f"tools={res.get('n_tool_calls')} text_len={res.get('text_len')} "
                  f"reasoning={res.get('reasoning_len')} "
                  f"thinking_blocks={res.get('n_thinking_blocks')}")
            if res.get("tool_calls"):
                c = res["tool_calls"][0]
                print(f"           tool          : {c['name']}({str(c['arguments'])[:80]})")
            if res.get("error"):
                print(f"           error         : {res['error'][:160]}")

    if not args.offline:
        a = report["lanes"].get("anthropic", {}).get("classification")
        o = report["lanes"].get("openai", {}).get("classification")
        if a and o:
            if a in {"EMPTY", "REASONING_ONLY"} and o == "TOOL_CALLS":
                print("REPRO: SDK anthropic lane lost the tool call that raw litellm got")
            elif a == "TOOL_CALLS" and o == "TOOL_CALLS":
                print("VERDICT: NO-REPRO at SDK layer with these settings "
                      "(see FINDINGS.md for the hypothesis this refutes)")
            else:
                print(f"VERDICT: MIXED anthropic={a} openai={o}")
    print(f"COST: {0 if args.offline else len(wanted)} API call(s) spent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
