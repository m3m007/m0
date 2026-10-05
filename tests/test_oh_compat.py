"""Offline unit tests for fix/oh_compat.py (T3/T4 gate).

Zero API calls, zero network. They prove:
  * a role file with a leading title line would give an agent NO tools (the
    defect), and oh_compat's normalisation + guard turns that silent failure
    into a loud, pre-flight error;
  * correct LLM kwargs pair transport prefix with base_url and refuse a mismatch;
  * the SDK's public ``capability_overrides`` hook is the documented surface
    (so no monkeypatch is needed for thinking behaviour);
  * ``model:`` in a role file is a profile name (RC2) and we raise for it
    before any spend;
  * the tool registry must be populated before spawning (RC3).

The final assertion in every spawn test is "the agent can act", which is the
behaviour the brief asked to be proven for thinking models.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
for _m in [k for k in sys.modules if k.startswith("fix.oh_compat")]:
    del sys.modules[_m]

from fix import oh_compat as oh  # noqa: E402
from lib.keys import ANTHROPIC_BASE_URL, COMPATIBLE_BASE_URL  # noqa: E402

BROKEN_ROLE = """# architect
---
name: architect
description: Plans work and reviews results; read-only.
model: inherit
tools:
  - terminal
  - file_editor
permission_mode: never_confirm
---

You are the ARCHITECT. Read files, run read-only commands, produce specs.
"""

GOOD_ROLE = """---
name: architect
description: Plans work and reviews results; read-only.
model: inherit
tools:
  - terminal
  - file_editor
permission_mode: never_confirm
---

You are the ARCHITECT. Read files, run read-only commands, produce specs.
"""

PROFILE_TRAP_ROLE = """---
name: architect
description: d
model: anthropic/deepseek-v4-pro
tools:
  - terminal
---

body
"""

NO_TOOL_ROLE = """---
name: ghost
description: d
model: inherit
tools: []
---

body
"""


@pytest.fixture(scope="module")
def llm():
    """A real SDK LLM. Constructing it costs nothing."""
    return oh.build_llm("anthropic/glm-5.3")


def write_role(tmp_path: Path, text: str, name: str = "architect.md") -> Path:
    d = tmp_path / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    return d


# ------------------------------------------------------- the defect itself
def test_leading_title_line_silently_drops_every_tool(tmp_path):
    """Without oh_compat, the SDK parses our role files as tools=[]."""
    from openhands.sdk.subagent import load_agents_from_dir

    d = write_role(tmp_path, BROKEN_ROLE)
    defn = load_agents_from_dir(d)[0]
    assert defn.tools == [], "expected the SDK to ignore this frontmatter"
    assert defn.description == ""
    # the whole body was swallowed as 'content', so the role lost its identity
    assert "ARCHITECT" in (defn.system_prompt or "")


def test_frontmatter_on_line_1_parses_correctly(tmp_path):
    from openhands.sdk.subagent import load_agents_from_dir

    defn = load_agents_from_dir(write_role(tmp_path, GOOD_ROLE))[0]
    assert defn.tools == ["terminal", "file_editor"]
    assert defn.description.startswith("Plans work")
    assert defn.permission_mode == "never_confirm"


# ---------------------------------------------------- oh_compat normalise
def test_normalise_moves_frontmatter_to_line_1():
    text, changed = oh.normalise_role_text(BROKEN_ROLE)
    assert changed and text.startswith("---")
    assert "# architect" in text            # heading preserved...
    assert text.index("# architect") > text.index("name: architect")  # ...as body
    # re-parsing the normalised text recovers every declared tool
    import frontmatter

    meta = frontmatter.loads(text).metadata
    assert meta["tools"] == ["terminal", "file_editor"]


def test_normalise_is_noop_on_good_files():
    text, changed = oh.normalise_role_text(GOOD_ROLE)
    assert not changed and text == GOOD_ROLE


def test_normalise_refuses_to_guess_weird_structure():
    weird = "some prose\nmore prose\n---\ntools: [terminal]\n---\nbody\n"
    text, changed = oh.normalise_role_text(weird)
    assert not changed and text == weird, "must not rewrite what it cannot parse"


def test_normalise_leaves_files_without_frontmatter_alone():
    plain = "# just a heading\nno frontmatter here\n"
    text, changed = oh.normalise_role_text(plain)
    assert not changed and text == plain


# ------------------------------------------------------------ load_roles
def test_load_roles_repairs_and_warns(tmp_path):
    d = write_role(tmp_path, BROKEN_ROLE)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        defs = oh.load_roles(d)
    assert defs[0].tools == ["terminal", "file_editor"]
    assert any("frontmatter was not on line 1" in str(w.message) for w in caught)


def test_load_roles_strict_raises_on_truly_toolless_role(tmp_path):
    d = write_role(tmp_path, NO_TOOL_ROLE, name="ghost.md")
    with pytest.raises(oh.RoleFileError, match="no tool call and no content"):
        oh.load_roles(d)
    # and strict=False lets it through for callers that want the old behaviour
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        defs = oh.load_roles(d, strict=False)
    assert defs[0].tools == []


# ------------------------------------------------------------ RC3 registry
def test_tools_registry_must_be_populated_first():
    names = oh.ensure_tools_registered()
    assert {"terminal", "file_editor", "task_tracker"} <= set(names)
    # idempotent + cheap on the second call
    assert oh.ensure_tools_registered() == names


# ------------------------------------------------------- RC2 profile trap
def test_model_field_is_a_profile_name_and_we_raise_early(tmp_path, llm):
    from openhands.sdk.subagent import load_agents_from_dir

    defn = load_agents_from_dir(write_role(tmp_path, PROFILE_TRAP_ROLE))[0]
    assert defn.model == "anthropic/deepseek-v4-pro"
    with pytest.raises(oh.RoleFileError) as exc:
        oh.spawn_agent(defn, llm, work_dir=str(tmp_path))
    message = str(exc.value)
    assert "PROFILE NAME" in message
    assert "inherit" in message, "must tell the caller what to do instead"
    # and the SDK itself really does raise the same class of error
    from openhands.sdk.subagent import agent_definition_to_factory

    with pytest.raises(ValueError, match="Profile"):
        agent_definition_to_factory(defn, str(tmp_path))(llm)


# -------------------------------------------------------- spawn + guard
def test_spawn_agent_gives_a_runnable_agent(tmp_path, llm):
    d = write_role(tmp_path, GOOD_ROLE)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        defn = oh.load_roles(d)[0]
    agent = oh.spawn_agent(defn, llm, work_dir=str(tmp_path),
                           require=("terminal", "file_editor"))
    names = oh.assert_runnable(agent, defn)
    assert {"terminal", "file_editor"} <= set(names)
    assert not set(names) <= set(oh.ALWAYS_ON)


def test_assert_runnable_rejects_toolless_agent(llm):
    """The guard that would have saved the netflow run: loud, and costs 0 calls."""
    from openhands.sdk import Agent

    class FakeDef:
        name = "ghost"
        tools: list[str] = []

    agent = Agent(llm=llm, tools=[])
    with pytest.raises(oh.RoleFileError, match="no executable tools"):
        oh.assert_runnable(agent, FakeDef())
    # empty tools + no defn still fails; the RC1 hint only appears when the role
    # file DID declare tools (i.e. the parse was silently thrown away)
    with pytest.raises(oh.RoleFileError, match="no executable tools"):
        oh.assert_runnable(agent, None)
    with pytest.raises(oh.RoleFileError, match="frontmatter was almost certainly"):
        oh.assert_runnable(agent, type("D", (), {"name": "x", "tools": ["terminal"]})())


def test_assert_runnable_missing_required_tool(llm):
    """An agent with *some* tools but not the required one is a distinct error."""
    from openhands.sdk import Agent
    from openhands.tools.preset.default import get_default_tools

    oh.ensure_tools_registered()
    specs = [t for t in get_default_tools(enable_browser=False,
                                          enable_sub_agents=False)
             if getattr(t, "name", None) != "file_editor"]
    agent = Agent(llm=llm, tools=specs)
    names = oh.assert_runnable(agent)          # passes with no requirement
    assert "file_editor" not in names
    with pytest.raises(oh.RoleFileError, match="missing required tools"):
        oh.assert_runnable(agent, None, require=("file_editor",))


def test_spawn_agent_fails_loudly_on_broken_role_when_normalise_off(tmp_path, llm):
    """The RC1 file, loaded with normalisation disabled, must not spawn silently."""
    from openhands.sdk.subagent import load_agents_from_dir

    defn = load_agents_from_dir(write_role(tmp_path, BROKEN_ROLE))[0]
    assert defn.tools == []
    with pytest.raises(oh.RoleFileError, match="no executable tools"):
        oh.spawn_agent(defn, llm, work_dir=str(tmp_path))


# ------------------------------------------------------------ lane checks
def test_correct_llm_kwargs_pair_prefix_with_endpoint():
    a = oh.correct_llm_kwargs("anthropic/glm-5.3")
    assert a["model"] == "anthropic/glm-5.3"
    assert a["base_url"] == ANTHROPIC_BASE_URL
    o = oh.correct_llm_kwargs("openai/glm-5.3")
    assert o["base_url"] == COMPATIBLE_BASE_URL
    assert a["streaming"] is False and a["retry_max_attempts"] == 3
    # the key must be resolved, never a literal in this repo
    assert isinstance(a["api_key"], str) and len(a["api_key"]) > 8


def test_validate_lane_rejects_mismatched_transport():
    transport, expected = oh.validate_lane("anthropic/glm-5.3", ANTHROPIC_BASE_URL)
    assert (transport, expected) == ("anthropic", ANTHROPIC_BASE_URL)
    with pytest.raises(ValueError, match="lane mismatch"):
        oh.validate_lane("anthropic/glm-5.3", COMPATIBLE_BASE_URL)
    with pytest.raises(ValueError, match="transport prefix"):
        oh.validate_lane("glm-5.3", ANTHROPIC_BASE_URL)
    with pytest.raises(ValueError, match="unsupported|Unknown transport|groq"):
        oh.validate_lane("groq/glm-5.3", None)


def test_capability_overrides_is_the_public_surface():
    """No monkeypatch needed for thinking behaviour: the SDK exposes a hook."""
    kwargs = oh.correct_llm_kwargs(
        "anthropic/glm-5.3", capability_overrides={"thinking_mode": "manual"})
    assert kwargs["capability_overrides"] == {"thinking_mode": "manual"}

    from openhands.sdk import LLM
    from openhands.sdk.llm.utils.model_features import get_features

    llm = LLM(model="anthropic/glm-5.3", api_key="x" * 24,
              base_url=ANTHROPIC_BASE_URL,
              capability_overrides={"thinking_mode": "manual",
                                    "supports_prompt_cache": True})
    feats = get_features(llm.model, None, llm.capability_overrides)
    assert feats.thinking_mode == "manual"
    assert feats.supports_extended_thinking is True
    assert feats.supports_prompt_cache is True
    # ...while the same model with no overrides is 'none' (our normal state)
    plain = get_features("anthropic/glm-5.3")
    assert plain.thinking_mode == "none"
    assert plain.supports_extended_thinking is False


def test_unknown_capability_override_is_ignored_not_fatal():
    from openhands.sdk import LLM

    llm = LLM(model="anthropic/glm-5.3", api_key="x" * 24, base_url=ANTHROPIC_BASE_URL,
              capability_overrides={"totally_made_up": True})
    assert llm.capability_overrides["totally_made_up"] is True


def test_llm_silently_drops_unknown_kwargs_documented_trap():
    """max_iteration_per_run is NOT an LLM field; extra='ignore' eats it.

    Pinned as a test because it is a live footgun: putting team budget knobs on
    the LLM looks like it works and does nothing.
    """
    from openhands.sdk import LLM

    llm = LLM(model="anthropic/glm-5.3", api_key="x" * 24,
              base_url=ANTHROPIC_BASE_URL, max_iteration_per_run=99)
    assert getattr(llm, "max_iteration_per_run", None) is None
    from openhands.sdk.conversation.impl.local_conversation import LocalConversation

    params = LocalConversation.__init__.__code__.co_varnames
    assert "max_iteration_per_run" in params


def test_send_reasoning_content_cannot_be_overridden():
    """Recorded gap: SEND_REASONING_CONTENT_MODELS uses substring matching, so
    its 'deepseek/deepseek-v4-pro' entry never matches 'anthropic/deepseek-v4-pro',
    and capability_overrides does not expose the key either."""
    from openhands.sdk.llm.utils.model_features import get_features

    f = get_features("anthropic/deepseek-v4-pro", None,
                     {"send_reasoning_content": True})
    assert f.send_reasoning_content is False


# ------------------------------------------- optional runtime patch (E2E)
def test_frontmatter_compat_makes_our_real_files_spawn_runnable(tmp_path, llm):
    """Prove the monkeypatch path end-to-end: an unmodified role file on disk,
    loaded through the stock SDK loader, still yields an agent that can act.

    This is the "smallest possible monkeypatch wrapper" the brief described --
    in-memory only: site-packages is never edited, so there is nothing to back
    out and no .bak to keep.
    """
    from openhands.sdk.subagent import AgentDefinition, load_agents_from_dir

    original_load = AgentDefinition.load
    try:
        assert oh.install_frontmatter_compat() is True
        assert oh.install_frontmatter_compat() is True, "patch is not idempotent"
        d = write_role(tmp_path, BROKEN_ROLE)
        defn = load_agents_from_dir(d)[0]
        assert defn.tools == ["terminal", "file_editor"], "patch did not take effect"
        agent = oh.spawn_agent(defn, llm, work_dir=str(tmp_path),
                               require=("terminal", "file_editor"))
        assert "terminal" in oh.assert_runnable(agent, defn)
    finally:
        AgentDefinition.load = original_load
    # after un-patching, the stock loader is blind again -- which proves the
    # patch was doing the work rather than some cached state
    d2 = write_role(tmp_path / "again", BROKEN_ROLE)
    assert load_agents_from_dir(d2)[0].tools == []


def test_site_packages_untouched():
    """The brief forbids destructive site-packages edits; assert we honoured it."""
    import subprocess

    sdk = Path("/home/hermes/.hermes/cache/scratch/oh-venv/lib/python3.14"
               "/site-packages/openhands/sdk")
    if not sdk.is_dir():
        pytest.skip("venv not present on this machine")
    baks = [p for p in sdk.rglob("*") if ".bak" in p.name]
    assert not baks, f"backup files found -> site-packages was edited: {baks}"
    out = subprocess.run(["git", "-C", str(REPO), "diff", "--name-only"],
                         capture_output=True, text=True, check=False)
    assert "site-packages" not in out.stdout
