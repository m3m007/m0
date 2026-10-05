"""The A/B repro: same model, same tools, two transports. Costs 1 call each.

    python3 repro/ab_probe.py                        # BOTH lanes (2 calls)
    python3 repro/ab_probe.py --lane openai          # known-good only (1 call)
    python3 repro/ab_probe.py --thinking off         # does thinking toggle matter?

Lanes:
    anthropic  LLM(model="anthropic/glm-5.3",  base_url=".../apps/anthropic")
    openai     LLM(model="openai/glm-5.3",     base_url=".../compatible-mode/v1")

Success for a single-shot call with a tool offered = the message contains a
tool call. A turn with neither tool calls nor text is the bug from the brief.

Raw LiteLLM response is captured to runs/repro/ via capture.py, which redacts
every value before it can touch the disk.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from capture import (WEATHER_PROMPT, WEATHER_TOOL, capture, classify,  # noqa: E402
                     now_iso, record_calls)
from lib.keys import COMPATIBLE_BASE_URL, ANTHROPIC_BASE_URL, token_plan_key  # noqa: E402

import litellm  # noqa: E402

MODEL_ID = "glm-5.3"
LANES = {
    "anthropic": {"prefix": "anthropic", "base_url": ANTHROPIC_BASE_URL},
    "openai": {"prefix": "openai", "base_url": COMPATIBLE_BASE_URL},
}


def extra_body(thinking: str) -> dict | None:
    """Vendor toggle the owner's Qwen CLI already uses per model."""
    if thinking == "off":
        return {"enable_thinking": False}
    if thinking == "on":
        return {"enable_thinking": True}
    return None


def call_lane(name: str, thinking: str, with_tool: bool,
              model_id: str = MODEL_ID) -> dict:
    lane = LANES[name]
    model = f"{lane['prefix']}/{model_id}"
    messages = [{"role": "user", "content": WEATHER_PROMPT}]
    kwargs: dict = {
        "model": model,
        "api_key": token_plan_key(),
        "api_base": lane["base_url"],
        "messages": messages,
        "temperature": 0.0,
    }
    if with_tool:
        kwargs["tools"] = [WEATHER_TOOL]
        kwargs["tool_choice"] = "auto"
    body = extra_body(thinking)
    if body:
        kwargs["extra_body"] = body

    entry: dict = {"lane": name, "model": model, "api_base": lane["base_url"],
                   "with_tool": with_tool, "thinking": thinking,
                   "ts": now_iso(), "request_kwargs": dict(kwargs)}
    entry["request_kwargs"]["api_key"] = "<REDACTED>"
    try:
        resp = litellm.completion(**kwargs, max_tokens=512)
        choice = resp.choices[0]
        msg = choice.message
        raw = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
        entry.update({
            "ok": True,
            "finish_reason": choice.finish_reason,
            "has_tool_calls": bool(getattr(msg, "tool_calls", None)),
            "tool_calls": [
                {"name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (getattr(msg, "tool_calls", None) or [])
            ],
            "content_text": (msg.content or ""),
            "reasoning_content": getattr(msg, "reasoning_content", None),
            "thinking_blocks": getattr(msg, "thinking_blocks", None),
            "classification": classify(msg.content, getattr(msg, "tool_calls", None),
                                       getattr(msg, "reasoning_content", None)
                                       or getattr(msg, "thinking_blocks", None)),
            "stop_reason": getattr(choice, "stop_reason", None),
            "usage": raw.get("usage") if isinstance(raw, dict) else None,
            "raw_top_keys": sorted(raw.keys()) if isinstance(raw, dict) else None,
            "raw_choices0_keys": (sorted(raw["choices"][0].keys())
                                  if isinstance(raw, dict) and raw.get("choices") else None),
            "raw_message_keys": (sorted(raw["choices"][0]["message"].keys())
                                 if isinstance(raw, dict) and raw.get("choices") else None),
            "raw": raw,
        })
    except Exception as exc:
        entry.update({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                      "classification": "ERROR"})
    record_calls(model, 1)
    return entry


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lane", choices=["anthropic", "openai", "both"], default="both")
    ap.add_argument("--thinking", choices=["auto", "on", "off"], default="auto")
    ap.add_argument("--no-tools", action="store_true",
                    help="omit tools entirely: is the endpoint or the tool path broken?")
    ap.add_argument("--model", default=MODEL_ID, help="model id after the prefix")
    args = ap.parse_args(argv)

    model_id = args.model
    wanted = ["anthropic", "openai"] if args.lane == "both" else [args.lane]
    results = [call_lane(name, args.thinking, not args.no_tools, model_id)
               for name in wanted]
    tag = f"ab_{args.lane}_{args.thinking}{'_notools' if args.no_tools else ''}_{model_id}"
    capture(tag, {"results": results})

    print(f"\n--- repro capture: runs/repro/{tag}.json ---")
    for r in results:
        print(f"[{r['lane']:9s}] model={r['model']} tools={r['with_tool']} "
              f"thinking={r['thinking']}")
        if not r.get("ok"):
            print(f"           ERROR: {r.get('error')}")
            continue
        print(f"           finish={r['finish_reason']} class={r['classification']} "
              f"tool_calls={len(r['tool_calls'])} text_len={len(r['content_text'])} "
              f"reasoning={'yes' if r.get('reasoning_content') else 'no'} "
              f"thinking_blocks={'yes' if r.get('thinking_blocks') else 'no'}")
        if r["tool_calls"]:
            print(f"           tool: {r['tool_calls'][0]['name']}({r['tool_calls'][0]['arguments']})")

    anth = next((r for r in results if r["lane"] == "anthropic"), None)
    oai = next((r for r in results if r["lane"] == "openai"), None)
    if anth and oai:
        if anth["classification"] == "TOOL_CALLS" and oai["classification"] == "TOOL_CALLS":
            print("VERDICT: NO-REPRO both lanes returned tool calls")
        elif anth["classification"] in {"EMPTY", "REASONING_ONLY"} \
                and oai["classification"] == "TOOL_CALLS":
            print("REPRO: anthropic lane empty/reasoning-only, openai lane OK")
        else:
            print(f"VERDICT: MIXED anthropic={anth['classification']} openai={oai['classification']}")
    elif anth:
        print(f"VERDICT: anthropic={anth['classification']}")
    elif oai:
        print(f"VERDICT: openai={oai['classification']}")
    print(f"COST: {len(results)} API call(s) spent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
