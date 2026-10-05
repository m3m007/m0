"""Zero-cost probe: print the exact LiteLLM kwargs the SDK would send.

    python3 repro/show_kwargs.py            # costs 0 API calls
    python3 repro/show_kwargs.py --json

Calls ``LLM._prepare_transport_kwargs`` (openhands/sdk/llm/llm.py:2476), which
is the last thing to run before ``litellm.completion`` — so the dict printed
here *is* the request. No network, no quota, and every value passes through
``redact()`` before it is shown or written.

Why this exists: the brief's deliverable #1 asks for "raw LiteLLM kwargs". The
fastest way to get them is not to send a request; it is to ask the object that
builds the request.
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

from lib.keys import COMPATIBLE_BASE_URL, ANTHROPIC_BASE_URL, redact, token_plan_key  # noqa: E402

# The four lanes worth comparing. Same model, both transports, plus a real
# Anthropic model as the control the curated lists were written for.
LANES: list[tuple[str, str, str]] = [
    ("anthropic/glm-5.3", ANTHROPIC_BASE_URL, "suspect lane: thinking model via anthropic-messages"),
    ("openai/glm-5.3", COMPATIBLE_BASE_URL, "known-good lane: same model via chat/completions"),
    ("anthropic/deepseek-v4-pro", ANTHROPIC_BASE_URL, "suspect lane #2"),
    ("anthropic/claude-sonnet-4-5", ANTHROPIC_BASE_URL, "control: model the curated lists DO know"),
]


def build(model: str, base_url: str, **extra):
    """Instantiate the SDK LLM and ask it for its own outbound kwargs."""
    from openhands.sdk.llm import LLM

    llm = LLM(
        model=model,
        api_key=token_plan_key(),
        base_url=base_url,
        streaming=False,
        retry_multiplier=1.0,
        retry_min_wait=1,
        retry_max_wait=1,
        retry_max_attempts=1,
        **extra,
    )
    messages = [{"role": "user", "content": "ping"}]
    kwargs = llm._prepare_transport_kwargs(messages=messages, enable_streaming=False)
    return llm, kwargs


def features_of(model: str):
    from openhands.sdk.llm.utils.model_features import get_features

    return get_features(model)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="also write runs/repro/kwargs.json")
    parser.add_argument("--only", default=None, help="comma-separated model ids to inspect")
    args = parser.parse_args(argv)

    only = set((args.only or "").split(",")) if args.only else None
    report: dict[str, object] = {"api_key": "<REDACTED>", "lanes": {}}
    # sanity: the key must never appear inside what we are about to print
    real_key = token_plan_key()

    for model, base_url, note in LANES:
        if only and model not in only:
            continue
        llm, kwargs = build(model, base_url)
        feats = features_of(model)
        safe_kwargs = redact({k: v for k, v in kwargs.items() if k != "messages"})
        thinking_keys = sorted(
            k for k in kwargs
            if any(t in k.lower() for t in ("think", "reason", "budget", "effort"))
        )
        entry = {
            "note": note,
            "base_url": kwargs.get("api_base"),
            "model_sent": kwargs.get("model"),
            "drop_params": kwargs.get("drop_params"),
            "max_retries": kwargs.get("max_retries"),
            "thinking_related_kwargs": thinking_keys,
            "full_kwargs_keys": sorted(kwargs.keys()),
            "model_features": {
                "thinking_mode": feats.thinking_mode,
                "supports_extended_thinking": feats.supports_extended_thinking,
                "send_reasoning_content": feats.send_reasoning_content,
                "supports_reasoning_effort": feats.supports_reasoning_effort,
                "supports_prompt_cache": feats.supports_prompt_cache,
                "force_string_serializer": feats.force_string_serializer,
            },
            "kwargs": safe_kwargs,
        }
        report["lanes"][model] = entry  # type: ignore[index]
        print(f"=== {model}")
        print(f"    note               : {note}")
        print(f"    api_base           : {kwargs.get('api_base')}")
        print(f"    thinking kwargs    : {thinking_keys or '(none)'}")
        print(f"    drop_params        : {kwargs.get('drop_params')}")
        print(f"    features           : thinking_mode={feats.thinking_mode} "
              f"extended_thinking={feats.supports_extended_thinking} "
              f"send_reasoning_content={feats.send_reasoning_content} "
              f"prompt_cache={feats.supports_prompt_cache}")
        print(f"    kwargs key count   : {len(kwargs)}")

    assert real_key not in json.dumps(report, default=str), "leak in kwargs report"
    if args.json:
        out = REPO / "runs" / "repro"
        out.mkdir(parents=True, exist_ok=True)
        (out / "kwargs.json").write_text(json.dumps(report, indent=2, default=str),
                                         encoding="utf-8")
        print(f"\nwrote {out/'kwargs.json'} (redacted)")
    print("\nVERDICT: kwargs captured with 0 API calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
