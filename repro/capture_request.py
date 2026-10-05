"""Capture the EXACT body the SDK sends, at 0 API calls, then replay it.

    python3 repro/capture_request.py            # capture only, 0 calls
    python3 repro/capture_request.py --replay   # resend verbatim (1 call)
    python3 repro/capture_request.py --bisect   # drop one suspect key per try

Why an interceptor instead of another live probe: the failing lane returned HTTP
400 "Request body format invalid", and live probing to find out why costs a
request per hypothesis. Wrapping litellm.completion lets us read the real kwargs
for free; the raw call that works and the SDK call that fails can then be diffed
key by key.

Secrets: the captured dict is passed through lib.keys.redact() before anything is
printed or written. api_key is never shown -- an earlier ad-hoc debug print in
this repo leaked it to the terminal scrollback, which is exactly the mistake this
file is designed not to repeat.
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

from capture import capture, record_calls  # noqa: E402
from lib.keys import COMPATIBLE_BASE_URL, redact, token_plan_key  # noqa: E402

MODEL = os.environ.get("SMOKE_MODEL", "openai/qwen3.8-flash")
KEYS_WORTH_DROPPING = ("seed", "api_version", "timeout", "max_retries",
                       "drop_params", "custom_llm_provider", "tool_choice",
                       "parallel_tool_calls", "extra_body", "litellm_extra_body")


def install_capture() -> dict:
    """Wrap litellm.completion so the call is recorded and NOT sent."""
    import litellm

    box: dict = {}
    original = litellm.completion

    def spy(*args, **kwargs):
        box["args"] = list(args)
        box["kwargs"] = kwargs
        raise _StopHere("captured without sending")

    litellm.completion = spy
    box["restore"] = lambda: setattr(litellm, "completion", original)
    return box


class _StopHere(Exception):
    """Raised from the spy so no request leaves the machine."""


def build_and_capture(role: str = "fixer") -> dict:
    from team.run_team import Team

    box = install_capture()
    try:
        team = Team(REPO, dry_run=False)
        agent, llm, lane = team.build_agent("fixer")
        from openhands.sdk import Conversation
        from openhands.tools.preset.default import get_default_tools

        conv = Conversation(agent=agent,
                            workspace=str(REPO / "runs" / "capture_ws"),
                            max_iteration_per_run=1)
        conv.send_message("List the files here using the terminal tool.")
        try:
            conv.run()
        except Exception:
            pass                       # the spy always raises; that is the point
    finally:
        box["restore"]()
    kwargs = box.get("kwargs") or {}
    return {"lane": lane.name, "model": lane.model,
            "captured_keys": sorted(kwargs),
            "kwargs": redact(kwargs),
            "n_messages": len(kwargs.get("messages") or []),
            "roles": [m.get("role") for m in (kwargs.get("messages") or [])],
            "n_tools": len(kwargs.get("tools") or [])}


def replay(kwargs: dict, drop: tuple[str, ...] = ()) -> dict:
    """Send the captured request verbatim (minus dropped keys). 1 call."""
    import litellm

    live = dict(kwargs)
    live["api_key"] = token_plan_key()      # redacted copy -> real key restored
    for key in drop:
        live.pop(key, None)
    out = {"dropped": list(drop), "keys_sent": sorted(live)}
    try:
        resp = litellm.completion(**live)
        msg = resp.choices[0].message
        out.update({"ok": True, "finish": resp.choices[0].finish_reason,
                    "tool_calls": len(msg.tool_calls or []),
                    "text_len": len(msg.content or "")})
    except Exception as exc:
        out.update({"ok": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "status": getattr(exc, "status_code", None)})
    record_calls(MODEL, 1)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", action="store_true", help="resend verbatim (1 call)")
    ap.add_argument("--bisect", action="store_true",
                    help="drop one suspect key at a time until the 400 goes away")
    args = ap.parse_args(argv)

    Path(REPO / "runs" / "capture_ws").mkdir(parents=True, exist_ok=True)
    info = build_and_capture()
    kwargs = info["kwargs"]
    report = {"captured": info}
    print("--- captured SDK request (0 calls) ---")
    print(f"lane          : {info['lane']} {info['model']}")
    print(f"keys          : {info['captured_keys']}")
    print(f"messages      : {info['n_messages']} roles={info['roles']}")
    print(f"tools         : {info['n_tools']}")

    if args.replay:
        report["replay"] = replay(kwargs)
        r = report["replay"]
        print(f"\nreplay verbatim: {'OK' if r.get('ok') else 'FAIL ' + str(r.get('error'))[:200]}")

    if args.bisect:
        results = []
        for key in KEYS_WORTH_DROPPING:
            if key not in kwargs:
                continue
            outcome = replay(kwargs, drop=(key,))
            results.append({"dropped": key, "ok": outcome.get("ok"),
                            "error": str(outcome.get("error", ""))[:160]})
            print(f"  drop {key:22s} -> {'OK' if outcome.get('ok') else 'still 400'}")
            if outcome.get("ok"):
                print(f"CAUSE: dropping {key!r} fixed the request")
                break
        report["bisect"] = results
    elif not args.replay:
        print("\n(no --replay/--bisect: nothing was sent, cost 0 calls)")

    path = capture("captured_request", report)
    print(f"capture: {path.relative_to(REPO)}")
    spent = 1 if args.replay else len(report.get("bisect", []))
    print(f"COST: {spent} API call(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
