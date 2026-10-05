"""Shared plumbing for repro/ — capture writing, call accounting, tool fixture.

Everything that lands on disk goes through :func:`lib.keys.redact` first, and
every capture is written under ``runs/`` (gitignored). There is no code path
here that can write an unredacted payload.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.keys import redact  # noqa: E402

RUNS = REPO / "runs" / "repro"
TOKENWATCH = Path.home() / ".hermes" / "scripts" / "tokenwatch.py"

# A deliberately tiny, deterministic tool. Same name/schema everywhere so the
# only variable between probes is the model/transport, never the tooling.
WEATHER_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "description": "City name, e.g. Osaka"},
    },
    "required": ["city"],
}
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up the current weather for a city. Always call this.",
        "parameters": WEATHER_SCHEMA,
    },
}
WEATHER_PROMPT = "What is the weather in Osaka right now? Use the get_weather tool."


def ensure_runs() -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    return RUNS


def capture(name: str, payload: Any) -> Path:
    """Write ``runs/repro/<name>.json`` with every secret scrubbed. Returns path."""
    ensure_runs()
    path = RUNS / f"{name}.json"
    safe = redact(payload)
    path.write_text(json.dumps(safe, indent=2, default=str, ensure_ascii=False),
                    encoding="utf-8")
    return path


def text_capture(name: str, text: str) -> Path:
    ensure_runs()
    path = RUNS / f"{name}.txt"
    from lib.keys import scrub_text
    path.write_text(scrub_text(text), encoding="utf-8")
    return path


def record_calls(model: str, calls: int = 1, source: str = "multiagent-stack:repro") -> None:
    """Log spent requests into the owner's tokenwatch ledger, if it exists.

    Quota on these plans is counted in REQUESTS, so the ledger is how we keep a
    multi-agent run inside its budget (brief: target < 120 calls per team run).
    """
    if calls <= 0:
        return
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("tokenwatch", TOKENWATCH)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.record(source, model, calls)
    except Exception as exc:  # pragma: no cover - ledger is best-effort
        print(f"[capture] tokenwatch record failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def classify(content: Any, tool_calls: Any, reasoning: Any = None) -> str:
    """Mirror of the SDK's own classification, for readable verdicts.

    Kept separate from ``openhands.sdk`` so this module imports offline.
    Priority matches agent/response_dispatch.py:54 (tool_calls > text >
    reasoning-only > empty).
    """
    if tool_calls:
        return "TOOL_CALLS"
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(getattr(item, "text", "") or ""))
        text = "".join(parts)
    if text and text.strip():
        return "CONTENT"
    if reasoning:
        return "REASONING_ONLY"
    return "EMPTY"
