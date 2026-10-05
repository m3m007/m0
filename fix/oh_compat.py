"""oh_compat — correct wiring + fail-fast guards for OpenHands sub-agent teams.

Importable and non-destructive: it NEVER edits site-packages. The optional
runtime patch (:func:`install_frontmatter_compat`) rebinds an in-memory
classmethod only, so there is no file edit to back out and no ``.bak`` needed.

WHY THIS FILE EXISTS
    The netflow team run died in a "LLM response contained no tool call and no
    content" loop (agent/response_dispatch.py:365-371). The brief blamed
    thinking-model handling in ``llm/utils/model_features.py``. That is not it:
    both Token Plan transports return proper tool calls (repro/ab_probe.py,
    repro/raw_curl.sh) and a full agent turn completes on ``anthropic/glm-5.3``
    (repro/sdk_conversation.py). The real chain was three usage errors:

    RC1  Role Markdown frontmatter not on line 1. Our files begin
         ``# architect`` then ``---``, so python-frontmatter sees no metadata;
         ``_extract_tools`` returns ``[]`` and the sub-agent spawns with only
         the built-in finish/think. NOTHING RAISES — that silence is the defect
         we compensate for.   (subagent/schema.py:338, schema.py:57)
    RC2  ``model:`` in a role file is a PROFILE NAME, not a model id.
         ``model: anthropic/deepseek-v4-pro`` raises
         ``ValueError: Profile ... not found in profile store``
         (subagent/registry.py:216-226). RC1 hid RC2: with metadata ignored,
         model fell back to ``inherit``, so fixing RC1 alone trades a silent
         loop for a hard crash. Both must be fixed together.
    RC3  The tool registry is populated lazily by importing the tools preset.
         Spawning before that import raises ``Tool 'terminal' not registered``
         (subagent/registry.py:243-250).

    ORDER THAT WORKS: register tools (RC3) -> normalise the role file (RC1) ->
    keep ``model: inherit`` (RC2) -> assert the spawned agent can act -> run.
"""

from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from lib.keys import base_url, token_plan_key  # noqa: E402

#: Tools a role file may legitimately expect to exist.
KNOWN_TOOLS = ("terminal", "file_editor", "task_tracker", "task", "finish", "think")
#: What an agent always has, even with an empty tool spec.
ALWAYS_ON = frozenset({"finish", "think"})

__all__ = ["ALWAYS_ON", "KNOWN_TOOLS", "RoleFileError", "assert_runnable",
           "build_llm", "correct_llm_kwargs", "ensure_tools_registered",
           "install_frontmatter_compat", "load_roles", "normalise_role_text",
           "spawn_agent", "transport_for", "validate_lane"]


class RoleFileError(RuntimeError):
    """A role file would silently produce an agent that cannot act."""


# ---------------------------------------------------------------- RC3
def ensure_tools_registered(*, enable_browser: bool = False,
                            enable_sub_agents: bool = True) -> list[str]:
    """Populate the SDK tool registry; return registered names.

    ``agent_definition_to_factory`` validates each declared tool against
    ``list_registered_tools()``, and that registry is filled only as a side
    effect of importing the tools preset. Call this before spawning anything.
    """
    from openhands.sdk.tool.registry import list_registered_tools

    if "terminal" in list_registered_tools():
        return sorted(list_registered_tools())
    from openhands.tools.preset.default import get_default_tools

    get_default_tools(enable_browser=enable_browser,
                      enable_sub_agents=enable_sub_agents)
    return sorted(list_registered_tools())


# ---------------------------------------------------------------- RC1
def normalise_role_text(text: str) -> tuple[str, bool]:
    """Move YAML frontmatter to line 1 when a single title line precedes it.

    Returns ``(text, changed)``. The leading ``# architect`` heading is prose,
    not data, so it is demoted to the top of the body. A file with no
    frontmatter block is returned untouched with ``changed=False``, so callers
    can still complain about it.

    Implemented line-wise rather than with offsets: an earlier slice-arithmetic
    version silently dropped the closing ``---`` and produced a file that parsed
    as *no* metadata -- exactly the class of bug this function exists to fix.
    """
    if text.startswith("---"):
        return text, False

    lines = text.split("\n")
    # A frontmatter block is the FIRST delimiter pair that opens after line 0.
    # Anything later (e.g. a markdown horizontal rule inside the body) is not
    # frontmatter and must not be treated as one.
    delim = [i for i, ln in enumerate(lines) if ln.strip() in ("---", "===")]
    delim = [i for i in delim if i > 0]
    if len(delim) < 2:
        return text, False
    start, end = delim[0], delim[1]
    if lines[start].strip() != lines[end].strip():
        return text, False  # mismatched delimiters: refuse to guess

    preamble = "\n".join(lines[:start]).strip()
    if preamble and not (preamble.count("\n") == 0 and preamble.startswith("#")):
        return text, False  # unexpected structure: refuse to guess

    fm_block = "\n".join(lines[start:end + 1])
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    rebuilt = fm_block + "\n"
    if preamble:
        rebuilt += preamble + "\n"
    rebuilt += body
    return rebuilt, True


def _loads_def(text: str, origin: Path) -> Any:
    """Build an AgentDefinition from normalised text using the SDK's schema."""
    import frontmatter

    from openhands.sdk.subagent import AgentDefinition

    post = frontmatter.loads(text)
    payload = dict(post.metadata or {})
    payload.setdefault("name", origin.stem)
    payload["system_prompt"] = (post.content or "").strip()
    payload["source"] = str(origin)
    return AgentDefinition(**payload)


def load_roles(agents_dir: str | Path, *, strict: bool = True,
               normalise: bool = True) -> list[Any]:
    """Load role definitions, refusing to hand back tool-less agents.

    strict=True raises :class:`RoleFileError` when a role parses with zero
    tools — the exact silent condition that produced the corrective-feedback
    loop. ``normalise`` repairs the leading-title problem in memory first.
    """
    agents_dir = Path(agents_dir)
    if not agents_dir.is_dir():
        raise RoleFileError(f"agents dir not found: {agents_dir}")

    ensure_tools_registered()
    defs: list[Any] = []
    for md in sorted(agents_dir.glob("*.md")):
        if md.name.lower() == "readme.md":
            continue
        raw = md.read_text(encoding="utf-8")
        text, changed = normalise_role_text(raw) if normalise else (raw, False)
        if changed:
            warnings.warn(
                f"{md.name}: frontmatter was not on line 1; the SDK parser reads "
                "ZERO tools from such a file. oh_compat normalised it in memory — "
                "fix the file, or call install_frontmatter_compat().", stacklevel=2)
        try:
            defn = _loads_def(text, md) if changed else _sdk_load(md)
        except Exception as exc:
            raise RoleFileError(f"{md.name}: {type(exc).__name__}: {exc}") from exc
        if strict and not list(defn.tools or []):
            raise RoleFileError(
                f"{md.name}: parsed tools=[] — an agent from this file could only "
                f"call {sorted(ALWAYS_ON)} and would spin in the 'no tool call and "
                "no content' loop.")
        defs.append(defn)
    return defs


def _sdk_load(path: Path) -> Any:
    from openhands.sdk.subagent import AgentDefinition

    return AgentDefinition.load(path)


def install_frontmatter_compat() -> bool:
    """OPTIONAL runtime patch: let the SDK tolerate a leading title line.

    Wraps ``AgentDefinition.load`` so files shaped like ours parse even when
    loaded by code we do not control. In-memory only — site-packages is never
    modified. Idempotent. Returns True when active.
    """
    from openhands.sdk.subagent import AgentDefinition

    if getattr(AgentDefinition.load, "_oh_compat", False):
        return True
    original = AgentDefinition.load

    def patched(cls, agent_path, *args, **kwargs):  # noqa: ANN001
        path = Path(agent_path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return original(cls, path, *args, **kwargs)
        text, changed = normalise_role_text(raw)
        if not changed:
            return original(cls, path, *args, **kwargs)
        return _loads_def(text, path)

    patched._oh_compat = True  # type: ignore[attr-defined]
    AgentDefinition.load = classmethod(patched)  # type: ignore[method-assign]
    return True


# ------------------------------------------------------ lane validation
def transport_for(model: str) -> str:
    """'openai/glm-5.3' -> 'openai'."""
    if "/" not in model:
        raise ValueError(f"model {model!r} must carry a LiteLLM transport prefix")
    return model.split("/", 1)[0].lower()


def validate_lane(model: str, url: str | None) -> tuple[str, str]:
    """Return ``(transport, expected_base_url)``; raise if prefix and URL disagree.

    ``openai/x`` must point at compatible-mode and ``anthropic/x`` at
    apps/anthropic. Getting this backwards usually answers with an opaque 400
    that says nothing about prefixes, so we check it ourselves, up front.
    """
    head = transport_for(model)
    expected = base_url(head)
    if url:
        normalised = url.rstrip("/")
        if normalised and normalised != expected.rstrip("/"):
            raise ValueError(
                f"lane mismatch: model={model!r} expects {expected!r}, got {url!r}")
    return head, expected


def correct_llm_kwargs(model: str, *, temperature: float = 0.0,
                       capability_overrides: dict[str, Any] | None = None) -> dict:
    """The ``LLM(...)`` kwargs that actually work, as a splattable dict.

    Grounded in SDK source:
      * ``capability_overrides`` is the public hook the brief asked about
        (llm.py:459). Keys include ``thinking_mode`` (adaptive|manual|none|
        unknown), ``supports_reasoning_effort``, ``supports_prompt_cache``,
        ``supports_stop_words``, ``supports_responses_api``, ``supports_vision``.
        Overrides beat LiteLLM metadata and the curated lists.
      * ``send_reasoning_content`` is NOT overridable there — it comes only from
        ``SEND_REASONING_CONTENT_MODELS`` (model_features.py:294) via
        case-insensitive SUBSTRING matching, so its ``deepseek/deepseek-v4-pro``
        entry never matches our id ``anthropic/deepseek-v4-pro``. Harmless for
        tool calling, but a real gap; recorded rather than worked around.
      * ``max_iteration_per_run`` / ``max_budget_per_run`` are NOT LLM fields and
        LLM uses ``extra="ignore"``, so passing them silently does nothing.
        ``max_iteration_per_run`` belongs on Conversation; ``max_budget_per_run``
        and ``prompt_cache_key`` only exist on LocalConversation.
      * ``streaming=False`` removes SSE-parsing variance; correct for headless.
    """
    _, expected = validate_lane(model, None)
    kwargs: dict[str, Any] = {
        "model": model,
        "api_key": token_plan_key(),   # resolved at runtime, never hardcoded
        "base_url": expected,
        "streaming": False,
        "temperature": temperature,
        "drop_params": True,
        "retry_max_attempts": 3,
        "retry_multiplier": 2.0,
        "retry_min_wait": 2,
        "retry_max_wait": 30,
    }
    if capability_overrides:
        kwargs["capability_overrides"] = dict(capability_overrides)
    return kwargs


def build_llm(model: str, **overrides) -> Any:
    """Construct the SDK LLM with the correct kwargs for ``model``."""
    from openhands.sdk import LLM

    return LLM(**correct_llm_kwargs(model, **overrides))


# ---------------------------------------------------------- spawn + assert
def assert_runnable(agent: Any, defn: Any | None = None,
                    *, require: tuple[str, ...] = ()) -> list[str]:
    """Raise if the agent cannot do the work its role file promised.

    This is the guard whose absence let RC1 burn hours of quota: a sub-agent
    with an empty tool set still *constructs* fine, then every turn comes back
    with neither text nor a tool call, and the framework loops on corrective
    feedback. Failing here costs 0 API calls; failing there costs a run.
    """
    names = sorted({getattr(t, "name", str(t)) for t in (agent.tools or [])})
    declared = list(getattr(defn, "tools", []) or []) if defn is not None else []

    if set(names) <= set(ALWAYS_ON):
        detail = ""
        if declared:
            detail = (f" The role file declared {declared}, so its frontmatter "
                      "was almost certainly not parsed (see RC1).")
        raise RoleFileError(
            f"agent has no executable tools beyond {sorted(ALWAYS_ON)}; it would "
            f"spin in the 'no tool call and no content' loop.{detail}")

    missing = [t for t in require if t not in names]
    if missing:
        raise RoleFileError(f"agent is missing required tools: {missing} "
                            f"(has {names})")
    return names


def spawn_agent(defn: Any, parent_llm: Any, *, work_dir: str | Path | None = None,
                require: tuple[str, ...] = ()) -> Any:
    """Build the sub-agent for a role definition, with all three guards applied.

    RC2 is handled before spawning: a non-``inherit`` ``model:`` is a profile
    name, so if it is not present in the profile store we raise here with the
    list of available profiles instead of letting registry.py fail mid-run.
    """
    from openhands.sdk.subagent import agent_definition_to_factory

    ensure_tools_registered()

    model_field = str(getattr(defn, "model", "inherit") or "inherit")
    if model_field not in {"", "inherit"}:
        from openhands.sdk.llm.llm_profile_store import LLMProfileStore

        store = LLMProfileStore(getattr(defn, "profile_store_dir", None))
        available = [n.removesuffix(".json") for n in store.list()]
        if model_field.removesuffix(".json") not in available:
            raise RoleFileError(
                f"role '{defn.name}' sets model={model_field!r}, but that field "
                "is a PROFILE NAME, not a model id (RC2). Available profiles: "
                f"{available or '(none — the store is empty)'} . Either create "
                "the profile or set 'model: inherit' and pick the model on the "
                "parent LLM / router lane.")

    factory = agent_definition_to_factory(defn, work_dir)
    agent = factory(parent_llm)
    assert_runnable(agent, defn, require=require)
    return agent


def agent_for_role(defn: Any, parent_llm: Any, **kw) -> Any:
    """Alias for :func:`spawn_agent` — reads better at team/ call sites."""
    return spawn_agent(defn, parent_llm, **kw)
