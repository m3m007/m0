"""Secret-safe access to the provider keys this stack is allowed to use.

THE RULE: a key value never lives in this repo, never gets printed, never gets
written into a captured log. It is read from the environment, or pulled out of
the owner's existing ``~/.hermes/config.yaml`` at runtime, and referenced by
environment-variable *name* everywhere else.

Lanes (from the mission brief, verified 2026-10-05):
  * main   -- Alibaba "Token Plan", ONE key, TWO endpoints (openai-compatible
              and anthropic-messages). Both work with the same key.
  * backup -- Kimi/Moonshot, emergency only, until roughly 2026-10-10.
  * nothing else is paid for here, and nothing new gets signed up for.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

TOKEN_PLAN_HOST = "token-plan.ap-southeast-1.maas.aliyuncs.com"
COMPATIBLE_BASE_URL = f"https://{TOKEN_PLAN_HOST}/compatible-mode/v1"
ANTHROPIC_BASE_URL = f"https://{TOKEN_PLAN_HOST}/apps/anthropic"
KIMI_BASE_URL = "https://api.kimi.com/coding/v1"

TOKEN_PLAN_ENV = "BAILIAN_TOKEN_PLAN_API_KEY"
KIMI_ENV = "KIMI_API_KEY"
HERMES_CONFIG = os.path.expanduser("~/.hermes/config.yaml")

__all__ = [
    "ANTHROPIC_BASE_URL",
    "COMPATIBLE_BASE_URL",
    "KIMI_BASE_URL",
    "REPLACEMENT",
    "TokenPlanKeyNotFound",
    "base_url",
    "describe",
    "kimi_key",
    "redact",
    "scrub_text",
    "token_plan_key",
    "token_plan_source",
]

KeyNotFound = type("KeyNotFound", (RuntimeError,), {})
"""Alias kept so callers can catch either name."""


class TokenPlanKeyNotFound(RuntimeError):
    """No environment variable and no config entry could supply the key."""


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------
def _from_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _unquote(literal: str) -> str:
    """Unquote a YAML scalar without evaluating it."""
    literal = literal.strip()
    if len(literal) >= 2 and literal[0] == literal[-1] and literal[0] in "\"'":
        return literal[1:-1]
    return re.split(r"\s+#", literal, maxsplit=1)[0].strip()


def _find_in_config(marker: str = "token-plan") -> tuple[str, str] | None:
    """Return ``(api_key, base_url)`` for the block mentioning ``marker``.

    Deliberately line-based: the owner's config is ~130 KB of YAML with anchors
    we do not need, and a full parse would drag unrelated providers into scope.
    We remember the most recent ``base_url`` and return the ``api_key`` that
    sits beside one naming our host.
    """
    if not os.path.exists(HERMES_CONFIG):
        return None
    last_base: str | None = None
    with open(HERMES_CONFIG, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("base_url:"):
                candidate = _unquote(line.split(":", 1)[1])
                if candidate.startswith("http"):
                    last_base = candidate
                continue
            if line.startswith("api_key:") and last_base and marker in last_base:
                value = _unquote(line.split(":", 1)[1])
                if value:
                    return value, last_base
    return None


def token_plan_key() -> str:
    """Return the Token Plan key. Env var wins, then the owner's config.yaml.

    The raised message names only the variable *name*, never a value, so it is
    safe to let an exception bubble into a log.
    """
    env_value = _from_env(TOKEN_PLAN_ENV)
    if env_value:
        return env_value
    found = _find_in_config()
    if found:
        return found[0]
    raise TokenPlanKeyNotFound(
        f"no Token Plan key available: set ${TOKEN_PLAN_ENV} or add an "
        f"api_key under a token-plan base_url in {HERMES_CONFIG}"
    )


def token_plan_source() -> str:
    """Where :func:`token_plan_key` will find its value (name, never value)."""
    if _from_env(TOKEN_PLAN_ENV):
        return f"env:${TOKEN_PLAN_ENV}"
    if _find_in_config():
        return f"file:{HERMES_CONFIG} (api_key beside token-plan base_url)"
    return "unavailable"


def kimi_key() -> str:
    """Backup lane. Environment only -- we do not go hunting for it."""
    env_value = _from_env(KIMI_ENV)
    if env_value:
        return env_value
    raise TokenPlanKeyNotFound(f"no Kimi key available: set ${KIMI_ENV} (backup lane only)")


def base_url(kind: str) -> str:
    """Endpoint for a transport ``kind``.

    One key works on both Token Plan endpoints; the transport is selected by the
    LiteLLM model *prefix* (``openai/`` vs ``anthropic/``), so the prefix and
    this URL must always agree. That pairing is the single most common way this
    stack breaks -- see ``repro/FINDINGS.md``.
    """
    normalized = (kind or "").strip().lower()
    if normalized in {"openai", "compatible", "compatible-mode", "chat",
                      "openai-compatible"}:
        return COMPATIBLE_BASE_URL
    if normalized in {"anthropic", "anthropic-messages", "messages", "claude"}:
        return ANTHROPIC_BASE_URL
    if normalized in {"kimi", "moonshot", "backup"}:
        return KIMI_BASE_URL
    raise ValueError(f"unknown transport kind {kind!r}")


def describe() -> Mapping[str, str]:
    """Safe summary for logs and docs. Contains no secret bytes."""
    value = None
    try:
        value = token_plan_key()
    except TokenPlanKeyNotFound:
        pass
    return {
        "token_plan_source": token_plan_source(),
        "token_plan_len": str(len(value)) if value else "0",
        # 4 chars is enough to confirm "which credential" and not enough to
        # reconstruct or to be useful to anyone reading a log.
        "token_plan_hint": (value[:4] + "\u2026") if value else "none",
        "compatible_base_url": COMPATIBLE_BASE_URL,
        "anthropic_base_url": ANTHROPIC_BASE_URL,
        "kimi_present": str(bool(_from_env(KIMI_ENV))),
    }


# --------------------------------------------------------------------------
# redaction -- the last line of defense before anything touches disk
# --------------------------------------------------------------------------
# Provider keys of the ``sk-...`` shape. Second char must be alphanumeric so
# prose like "sk-provider-key" is not mistaken for a credential.
_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._\-]{8,}\b")
# Long opaque blobs after an auth scheme.
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/]{12,}")
# A key sitting in a ``key: value`` pair, with optional matched quotes.
_KV_RE = re.compile(
    r"(?i)(?P<pre>['\"]?)(?P<k>api[_-]?key|authorization|x-api-key|"
    r"access[_-]?token|secret[_-]?key|token)(?P<post>['\"]?)"
    r"(?P<sep>\s*[:=]\s*)(?P<q>['\"]?)(?P<v>[^\s,'\"}\]]{4,})(?P=q)",
)
# Raw HTTP headers as they appear in a captured request/response dump.
_HEADER_RE = re.compile(r"(?im)^([ \t]*(?:authorization|x-api-key)[ \t]*:)[ \t]*\S.*$")
SENSITIVE_KEYS = {
    "api_key", "apikey", "api-key", "authorization", "x-api-key",
    "access_token", "secret", "secret_key", "token", "password",
}
REPLACEMENT = "<REDACTED>"

_LIVE_VALUES: tuple[str, ...] | None = None


def _live_secret_values() -> tuple[str, ...]:
    """Actual key strings in memory, for exact-substring scrubbing.

    Regexes can be evaded by formatting; an exact substring match cannot. This
    is why ``redact()`` never relies on shape alone. Values are cached, kept in
    process memory only, and never returned to a caller that would print them.
    """
    global _LIVE_VALUES
    if _LIVE_VALUES is None:
        collected: list[str] = []
        for getter in (lambda: os.environ.get(TOKEN_PLAN_ENV, ""),
                       token_plan_key, kimi_key):
            try:
                value = (getter() or "").strip()
            except Exception:
                continue
            if len(value) >= 8 and value not in collected:
                collected.append(value)
        _LIVE_VALUES = tuple(collected)
    return _LIVE_VALUES


def scrub_text(text: str) -> str:
    """Redact secrets from arbitrary raw text (log lines, bodies, dumps).

    Order: exact live-key substrings first (unforgeable), then header lines,
    then shape heuristics for keys we do not currently hold.
    """
    if not isinstance(text, str) or not text:
        return text
    out = text
    for value in _live_secret_values():
        if value in out:
            out = out.replace(value, REPLACEMENT)
    out = _HEADER_RE.sub(lambda m: m.group(1) + " " + REPLACEMENT, out)
    out = _SECRET_RE.sub(REPLACEMENT, out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REPLACEMENT}", out)
    out = _KV_RE.sub(
        lambda m: (
            f"{m.group('pre')}{m.group('k')}{m.group('post')}"
            f"{m.group('sep')}{m.group('q')}{REPLACEMENT}{m.group('q')}"
        ),
        out,
    )
    return out


def _scrub_container(container: Any, _depth: int = 0, _seen: set[int] | None = None):
    if _depth > 64:  # pathological nesting: keep it cheap, keep it safe
        return "<TRUNCATED-DEPTH>"
    if isinstance(container, str):
        return scrub_text(container)
    if isinstance(container, (bytes, bytearray)):
        return scrub_text(container.decode("utf-8", "replace"))
    marker = id(container)
    _seen = _seen or set()
    if marker in _seen:
        return "<CYCLE>"
    if isinstance(container, Mapping):
        _seen = _seen | {marker}
        result: MutableMapping[Any, Any] = {}
        for key, value in container.items():
            normalised = key.lower().replace(" ", "_").replace("-", "_") if isinstance(key, str) else ""
            if normalised in SENSITIVE_KEYS:
                result[key] = REPLACEMENT if value else value
            else:
                result[key] = _scrub_container(value, _depth + 1, _seen)
        return result
    if isinstance(container, (list, tuple)):
        scrubbed = [_scrub_container(item, _depth + 1, _seen) for item in container]
        return tuple(scrubbed) if isinstance(container, tuple) else scrubbed
    if isinstance(container, (set, frozenset)):
        return [_scrub_container(item, _depth + 1, _seen) for item in container]
    # pydantic models / dataclasses / plain objects
    for attr in ("model_dump", "dict"):
        dumper = getattr(container, attr, None)
        if callable(dumper):
            try:
                return _scrub_container(dumper(), _depth + 1, _seen)
            except Exception:
                break
    to_dict = getattr(container, "__dict__", None)
    if isinstance(to_dict, dict):
        return _scrub_container(to_dict, _depth + 1, _seen)
    return container


def redact(obj: Any) -> Any:
    """Return a secret-free deep copy of ``obj``. Never mutates the input.

    Apply this to EVERYTHING before it touches disk: LiteLLM kwargs, raw
    response bodies, exception messages, event dumps. Dict keys named like
    credentials are replaced outright; every remaining string is scrubbed both
    by exact live-key match and by shape heuristic.
    """
    return _scrub_container(obj)


def _demo() -> int:
    info = describe()
    print("token plan key source : " + info["token_plan_source"])
    print("token plan key length : " + info["token_plan_len"])
    print("token plan key hint   : " + info["token_plan_hint"] + "   (hint = first 4 chars only)")
    print("compatible endpoint   : " + info["compatible_base_url"])
    print("anthropic endpoint    : " + info["anthropic_base_url"])
    print("kimi backup present   : " + info["kimi_present"])
    print("live values known     : " + str(len(_live_secret_values())) + " (used for exact scrubbing)")

    sample = {
        "model": "anthropic/glm-5.3",
        "api_key": "sk-sp-" + "FAKE-EXAMPLE-NOT-REAL",
        "headers": {"Authorization": "Bearer " + "sk-FAKEBEARERNOTREAL01",
                    "x-api-key": "sk-FAKEXAPIKEYNOTREAL01"},
        "extra_body": {"enable_thinking": True},
        "nested": {"deeper": {"inner": {"api_key": "sk-FAKENESTEDNOTREAL0001"}}},
        "raw": "curl -H 'Authorization: Bearer sk-FAKEINTEXTNOTREAL0001' https://example",
    }
    print("\nredaction demo (input -> output):")
    print("  input keys : " + ", ".join(sorted(sample)))
    print("  output     : " + repr(redact(sample)))
    out = redact(sample)
    assert out["api_key"] == REPLACEMENT
    assert out["nested"]["deeper"]["inner"]["api_key"] == REPLACEMENT
    assert out["headers"]["Authorization"] == REPLACEMENT
    assert "FAKEINTEXT" not in out["raw"]
    assert sample["api_key"].startswith("sk-sp-"), "redact() mutated its input!"
    print("  ok: secrets replaced at every depth; input object unchanged")

    # the strongest proof: the owner's REAL key survives nowhere
    real = token_plan_key()
    leaky = f"request failed with key {real} at {COMPATIBLE_BASE_URL}"
    scrubbed = scrub_text(leaky)
    assert real not in scrubbed, "exact scrubbing FAILED"
    print("  ok: live key scrubbed from free text -> " + repr(scrubbed))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual smoke
    raise SystemExit(_demo())
