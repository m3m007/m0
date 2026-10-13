"""R4b-D7: file agents register in BOTH modes, caps are honored, poison heals.

D7 step 0 — THE PROBE, AND ITS FINDING. The brief asked one question before any
implementation: does a Conversation created with workspace=W see file agents
registered from W/.openhands/agents at send_message time? Answered offline
against the real openhands-sdk 1.54, zero LLM calls:

    ANSWER: YES for an absolute W — and the yes is a trap in three parts.
      1. LocalConversation._register_file_based_agents() runs on the FIRST
         send_message()/run() and calls register_file_agents(
         self.workspace.working_dir), which scans exactly {W}/.agents/agents and
         {W}/.openhands/agents. working_dir is the string teamkit handed
         Conversation, stored VERBATIM by LocalWorkspace (str() only: no
         resolve, no normalise — a trailing slash survives), so for an absolute
         workspace the SDK's directory and teamkit's directory are the same
         directory. (test_probe_conversation_sees_project_file_agents,
         test_probe_working_dir_is_verbatim)
      2. That visibility depends on the process cwd: a RELATIVE workspace makes
         the SDK scan cwd/<rel>. (test_probe_relative_working_dir_resolves_against_cwd)
      3. It registers with register_agent_if_absent, so a name the registry
         already holds from an earlier run in this process is KEPT and the
         edited file on disk is silently ignored. The lazy pass is absent-only;
         it never re-reads. (test_probe_lazy_registration_preserves_stale_entry)

    Consequence for the implementation: the SDK's pass is NOT sufficient. It
    answers "can a fresh name be discovered", not "is the registered definition
    the current file", and it resolves relative paths by cwd. So teamkit's own
    refresh (refresh_workspace_agents) runs in BOTH modes: it scans
    {ws}/.agents/agents and {ws}/.openhands/agents via load_agents_from_dir,
    force-pops those names from _agent_factories, re-registers from the CURRENT
    disk files, normalizes a poisoned `model:` to 'inherit' with a loud warning
    naming file+value, and clamps each definition's max_iteration_per_run to
    min(declared, budget_calls*3). test_refresh_undoes_what_the_lazy_pass_cannot
    is the difference, proven on the real registry.

Cap surface (D7b), read from the SDK rather than guessed: Task manager's
_create_task spawns the role's conversation with
`factory.definition.max_iteration_per_run or parent.max_iteration_per_run`, and
applies `definition.get_confirmation_policy()` to that child
(openhands/tools/task/manager.py). So the definition object IS where a per-role
iteration cap and permission_mode live — clamping it is what honors the cap, and
the recorded `caps` map is the audit trail.

Everything after the probe section stubs openhands (the tests/test_runrecord.py
pattern) so those runs work on any interpreter and cannot spend a request.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from team import teamkit as tk  # noqa: E402
from tests.test_runrecord import install_oh_stubs  # noqa: E402

#: install_oh_stubs() monkeypatches tk.preflight; the real function is captured
#: here (at import time, before any patch) so one test can exercise it directly.
REAL_PREFLIGHT = tk.preflight


ROLE_MD = """---
name: {name}
description: {name} role
model: {model}
tools:
{tools}
permission_mode: never_confirm
{cap_line}---

You are {name}.
"""


def role_file(dirpath, name="builder-helper", model="inherit",
              tools=("terminal",), cap=None):
    """Write one agent-definition md file, return its path."""
    dirpath = Path(dirpath)
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / f"{name}.md"
    p.write_text(ROLE_MD.format(
        name=name, model=model,
        cap_line=f"max_iteration_per_run: {cap}\n" if cap else "",
        tools="\n".join(f"  - {t}" for t in tools) or "  - terminal"))
    return p


def _fake_def(name, tools=("terminal",), model="inherit", cap=None, source=None):
    """Stand-in AgentDefinition: refresh_workspace_agents reads exactly these."""
    return types.SimpleNamespace(name=name, description=f"{name} role",
                                 tools=list(tools), model=model,
                                 max_iteration_per_run=cap, source=source,
                                 permission_mode="never_confirm")


def _registry():
    return sys.modules["openhands.sdk.subagent.registry"]._agent_factories


# --------------------------------------------------------------- the probe (0)
# These run against the REAL SDK (the repo-root conftest.py makes it importable
# on the gate interpreter). No Conversation object is constructed and no LLM is
# touched: _register_file_based_agents only reads `self.workspace`.
def test_probe_conversation_sees_project_file_agents(tmp_path):
    """FINDING 1: the SDK's lazy pass registers {W}/.openhands/agents and the
    factory is retrievable by name."""
    pytest.importorskip("openhands.sdk", reason="openhands-sdk not importable")
    from openhands.sdk.conversation.impl.local_conversation import LocalConversation
    from openhands.sdk.subagent import get_agent_factory, load_agents_from_dir
    from openhands.sdk.subagent.registry import _agent_factories
    from openhands.sdk.workspace.local import LocalWorkspace

    W = tmp_path / "w1"
    p = role_file(W / ".openhands" / "agents", name="probe-visible")
    defs = load_agents_from_dir(Path(p.parent))
    assert [d.name for d in defs] == ["probe-visible"], defs
    assert list(defs[0].tools) == ["terminal"] and defs[0].model == "inherit"
    assert get_agent_factory is not None

    conv = LocalConversation.__new__(LocalConversation)
    conv.workspace = LocalWorkspace(working_dir=str(W))
    try:
        LocalConversation._register_file_based_agents(conv)
        assert "probe-visible" in _agent_factories, "lazy pass did not register"
        assert get_agent_factory("probe-visible").definition.name == "probe-visible"
    finally:
        _agent_factories.pop("probe-visible", None)


def test_probe_working_dir_is_verbatim(tmp_path):
    """FINDING 1b: working_dir is stored unchanged (trailing slash included), so
    teamkit must not assume the SDK normalised it."""
    pytest.importorskip("openhands.sdk", reason="openhands-sdk not importable")
    from openhands.sdk.workspace.local import LocalWorkspace

    W = tmp_path / "w2"
    W.mkdir()
    assert LocalWorkspace(working_dir=str(W)).working_dir == str(W)
    assert LocalWorkspace(working_dir=f"{W}/").working_dir == f"{W}/"
    dirs = tk.workspace_agent_dirs(str(W))
    assert [d.relative_to(W).as_posix() for d in dirs] == \
        [".agents/agents", ".openhands/agents"], dirs


def test_probe_relative_working_dir_resolves_against_cwd(tmp_path, monkeypatch):
    """FINDING 2: with a relative workspace the SDK scans cwd/<rel>; teamkit's
    own scan is absolute, which is why the refresh is not redundant."""
    pytest.importorskip("openhands.sdk", reason="openhands-sdk not importable")
    from openhands.sdk.workspace.local import LocalWorkspace

    home = tmp_path / "home"
    (home / "rel").mkdir(parents=True)
    monkeypatch.chdir(home)
    assert LocalWorkspace(working_dir="rel").working_dir == "rel"
    assert tk.workspace_agent_dirs("rel")[0].is_absolute(), \
        "teamkit's dirs must not depend on cwd"


def test_probe_lazy_registration_preserves_stale_entry(tmp_path):
    """FINDING 3 (the reason D7 exists): register_file_agents is absent-only, so
    an edited file does NOT replace an already-registered name."""
    pytest.importorskip("openhands.sdk", reason="openhands-sdk not importable")
    from openhands.sdk.subagent import register_file_agents
    from openhands.sdk.subagent.registry import _agent_factories

    W = tmp_path / "w3"
    role_file(W / ".agents" / "agents", name="probe-stale", tools=("terminal",))
    register_file_agents(str(W))
    first = _agent_factories.get("probe-stale")
    assert first is not None and list(first.definition.tools) == ["terminal"]
    try:
        role_file(W / ".agents" / "agents", name="probe-stale",
                  tools=("terminal", "file_editor"))
        register_file_agents(str(W))
        second = _agent_factories.get("probe-stale")
        assert second is first, ("SDK kept the stale object: drift is silent, "
                                 "which is exactly what the force-pop fixes")
        assert list(second.definition.tools) == ["terminal"]
    finally:
        _agent_factories.pop("probe-stale", None)


def test_refresh_undoes_what_the_lazy_pass_cannot(tmp_path):
    """The fix, on the real registry: after refresh_workspace_agents the CURRENT
    file wins, and the poisoned-model heal happens in the same pass."""
    pytest.importorskip("openhands.sdk", reason="openhands-sdk not importable")
    from openhands.sdk.subagent import register_file_agents
    from openhands.sdk.subagent.registry import _agent_factories

    W = tmp_path / "w4"
    agents = W / ".agents" / "agents"
    f = role_file(agents, name="drifted", tools=("terminal",))
    register_file_agents(str(W))
    assert list(_agent_factories["drifted"].definition.tools) == ["terminal"]
    try:
        role_file(agents, name="drifted", tools=("terminal", "file_editor"))
        out, warns = tk.refresh_workspace_agents(str(W))
        assert out["names"] == ["drifted"] and warns == [], (out, warns)
        assert list(_agent_factories["drifted"].definition.tools) == \
            ["terminal", "file_editor"], "edited file must win on the next run"
        # and the file the SDK parsed is the one it reports
        assert out["defs"][0].source.replace("\\", "/").endswith(
            ".agents/agents/drifted.md"), out["defs"][0].source
        assert f.read_text().count("file_editor") == 1
    finally:
        _agent_factories.pop("drifted", None)


def test_refresh_keeps_cap_and_policy_on_the_registered_definition(tmp_path):
    """The metadata trap, proven on the real SDK: registering with a plain
    STRING description makes the SDK build a fresh, metadata-empty
    AgentDefinition, and the Task manager reads the iteration cap and the
    permission mode off that object — so a role's cap would silently stop
    existing. teamkit passes the definition itself, so the registry copy keeps
    both."""
    pytest.importorskip("openhands.sdk", reason="openhands-sdk not importable")
    from openhands.sdk.subagent.registry import (AgentFactory,
                                                 _agent_factories,
                                                 register_agent_if_absent)

    W = tmp_path / "w5"
    role_file(W / ".agents" / "agents", name="capped", cap=999)
    out, _ = tk.refresh_workspace_agents(str(W), cap=7)
    try:
        assert out["caps"] == {"capped": 7}, out
        reg = _agent_factories["capped"]
        assert reg.definition.max_iteration_per_run == 7, "cap survived"
        assert reg.definition.permission_mode == "never_confirm", "policy survived"
        assert list(reg.definition.tools) == ["terminal"], reg.definition.tools
        # and the counter-example: what a string description really does
        marker = object()
        register_agent_if_absent("string-described", lambda llm: marker,
                                 "just a description")
        assert _agent_factories["string-described"].definition \
            .max_iteration_per_run is None
        assert _agent_factories["string-described"].definition.tools == []
    finally:
        _agent_factories.pop("capped", None)
        _agent_factories.pop("string-described", None)
    assert AgentFactory is not None  # the NamedTuple the registry stores


# ------------------------------------------------------ offline: refresh unit
def install_subagent_stub(monkeypatch, defs_by_dir):
    """teamkit's whole openhands surface, with a loader that serves OUR dicts.

    `defs_by_dir` maps directory path -> definitions, so a test states exactly
    which file teamkit is supposed to have read. Registry semantics are the
    SDK's: register_agent_if_absent is absent-only (returns False on a name
    that already exists), which is what makes the force-pop observable.
    """
    calls = []
    install_oh_stubs(monkeypatch, calls)
    registry = sys.modules["openhands.sdk.subagent.registry"]
    registry._agent_factories = {}
    registry._registry_lock = threading.RLock()

    class Factory:
        def __init__(self, d):
            self.definition = d
            self.factory_func = lambda llm: f"agent:{d.name}"

    def register_agent_if_absent(name, factory_func, description):
        if name in registry._agent_factories:
            return False
        registry._agent_factories[name] = types.SimpleNamespace(
            factory_func=factory_func,
            definition=description if hasattr(description, "name")
            else factory_func.definition)
        calls.append(("register", name))
        return True

    sub = sys.modules["openhands.sdk.subagent"]
    monkeypatch.setattr(sub, "load_agents_from_dir",
                        lambda p, root=None: list(defs_by_dir.get(str(p), [])))
    monkeypatch.setattr(sub, "register_agent_if_absent", register_agent_if_absent)
    monkeypatch.setattr(sub, "agent_definition_to_factory",
                        lambda d, ws=None: Factory(d))
    return calls, registry


def test_refresh_scans_both_workspace_dirs(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    d1, d2 = ws / ".agents" / "agents", ws / ".openhands" / "agents"
    install_subagent_stub(monkeypatch, {str(d1): [_fake_def("a1")],
                                        str(d2): [_fake_def("a2")]})
    out, warns = tk.refresh_workspace_agents(str(ws))
    assert out["names"] == ["a1", "a2"], out
    assert warns == []
    assert sorted(_registry()) == ["a1", "a2"]


def test_refresh_first_dir_wins_on_duplicate_names(tmp_path, monkeypatch):
    """.agents/agents beats .openhands/agents (the SDK's documented priority)."""
    ws = tmp_path / "proj"
    d1, d2 = ws / ".agents" / "agents", ws / ".openhands" / "agents"
    install_subagent_stub(monkeypatch, {
        str(d1): [_fake_def("dup", tools=("terminal",))],
        str(d2): [_fake_def("dup", tools=("terminal", "file_editor"))]})
    out, _ = tk.refresh_workspace_agents(str(ws))
    assert out["names"] == ["dup"] and len(out["defs"]) == 1, out
    assert list(_registry()["dup"].definition.tools) == ["terminal"], "first wins"


def test_poisoned_model_normalizes_to_inherit_and_warns(tmp_path, monkeypatch,
                                                        caplog):
    """D7(c) #1: RC2 poison is HEALED, not fatal. The warning names BOTH the
    file and the value; the owner's file is not rewritten under their feet."""
    ws = tmp_path / "proj"
    poisoned = role_file(ws / ".openhands" / "agents", name="poisoned",
                         model="qwen3.8-flash")
    good = role_file(ws / ".openhands" / "agents", name="honest")
    fake_poison = _fake_def("poisoned", model="qwen3.8-flash", source=str(poisoned))
    fake_good = _fake_def("honest", source=str(good))
    install_subagent_stub(monkeypatch,
                          {str(ws / ".openhands" / "agents"):
                           [fake_poison, fake_good]})
    with caplog.at_level(logging.WARNING, logger="teamkit"):
        out, warns = tk.refresh_workspace_agents(str(ws))
    assert fake_poison.model == "inherit", "in-memory definition healed"
    assert fake_good.model == "inherit", "clean definition untouched"
    assert out["normalized"] == [{"name": "poisoned", "model": "qwen3.8-flash"}]
    assert warns and "poisoned" in warns[0] and "qwen3.8-flash" in warns[0], warns
    text = caplog.text
    assert str(poisoned) in text and "qwen3.8-flash" in text and "poisoned" in text, text
    assert poisoned.read_text().count("qwen3.8-flash") == 1, \
        "the run must not silently edit a file the owner owns"


def test_blank_or_inherit_model_is_not_poison(tmp_path, monkeypatch, caplog):
    """'' and 'inherit' both mean parent lane; neither may trigger a warning."""
    ws = tmp_path / "proj"
    defs = [_fake_def("blank", model=""), _fake_def("inh", model="inherit")]
    install_subagent_stub(monkeypatch, {str(ws / ".agents" / "agents"): defs})
    with caplog.at_level(logging.WARNING, logger="teamkit"):
        out, warns = tk.refresh_workspace_agents(str(ws))
    assert out["normalized"] == [] and warns == []
    assert [d.model for d in defs] == ["", "inherit"]
    assert caplog.records == [], caplog.text


def test_cap_clamps_permissive_frontmatter(tmp_path, monkeypatch):
    """D7(b): min(def, budget*3) lands on the definition and is returned."""
    ws = tmp_path / "proj"
    wide, tight, none = _fake_def("wide", cap=10_000), _fake_def("tight", cap=4), \
        _fake_def("none")
    install_subagent_stub(monkeypatch,
                          {str(ws / ".agents" / "agents"): [wide, tight, none]})
    out, warns = tk.refresh_workspace_agents(str(ws), cap=30)
    assert out["caps"] == {"wide": 30, "tight": 4, "none": 30}, out
    assert (wide.max_iteration_per_run, tight.max_iteration_per_run,
            none.max_iteration_per_run) == (30, 4, 30)
    assert warns == []


def test_clamp_and_heal_apply_to_the_registered_definition(tmp_path, monkeypatch):
    """The object in the registry is the object that was clamped + healed —
    otherwise the cap and the RC2 guard would be theatre."""
    ws = tmp_path / "proj"
    d = _fake_def("worker", model="deepseek-v4", cap=999,
                  source=str(ws / ".agents" / "agents" / "worker.md"))
    install_subagent_stub(monkeypatch, {str(ws / ".agents" / "agents"): [d]})
    tk.refresh_workspace_agents(str(ws), cap=12)
    reg = _registry()["worker"]
    assert reg.definition is d
    assert d.model == "inherit" and d.max_iteration_per_run == 12
    assert reg.definition.model == "inherit"


def test_registration_failure_is_loud_not_fatal(tmp_path, monkeypatch, caplog):
    """A file the SDK refuses to build a factory for must not abort the run."""
    ws = tmp_path / "proj"
    install_subagent_stub(monkeypatch,
                          {str(ws / ".agents" / "agents"): [_fake_def("broken")]})

    def boom(d, ws=None):
        raise ValueError("Tool 'not-a-tool' not registered")

    monkeypatch.setattr(sys.modules["openhands.sdk.subagent"],
                        "agent_definition_to_factory", boom)
    with caplog.at_level(logging.WARNING, logger="teamkit"):
        out, warns = tk.refresh_workspace_agents(str(ws))
    assert out["names"] == [], "a broken def is not reported as registered"
    assert warns and warns[0].startswith("register failed: broken"), warns
    assert "broken" in caplog.text


def test_unreadable_agent_dir_degrades_to_empty(tmp_path, monkeypatch):
    """No dir, no defs, no raise: builder mode on a bare workspace is normal."""
    calls, registry = install_subagent_stub(monkeypatch, {})
    out, warns = tk.refresh_workspace_agents(str(tmp_path / "nothing-here"))
    assert out == {"names": [], "normalized": [], "caps": {}, "defs": []}, out
    assert warns == [] and registry._agent_factories == {}


# ------------------------------------------------- offline: whole-run coverage
def test_builder_mode_registers_file_agents_and_honors_cap(tmp_path, monkeypatch):
    """D7 headline: roles='none' used to skip registration entirely.

    Proven on a real run_team() call: the workspace file agent reaches the
    registry, its declared cap is bounded by the run budget, the parent
    Conversation gets the same number, and config.json records all of it.
    """
    ws = tmp_path / "proj"
    agents = ws / ".openhands" / "agents"
    role_file(agents, name="helper", cap=1000)
    helper = _fake_def("helper", cap=1000, source=str(agents / "helper.md"))
    calls, registry = install_subagent_stub(monkeypatch, {str(agents): [helper]})
    res = tk.run_team(brief="B", workspace=str(ws), budget_calls=10,
                      roles="none", contract={"gate": "true"},
                      waive_lessons="unit test")
    assert res["status"] == "GATE_PASS"
    assert "helper" in registry._agent_factories, "builder mode registers files"
    assert helper.max_iteration_per_run == 30, "budget_calls*3 bounds the 1000"
    cfg = json.loads((Path(res["run_dir"]) / "config.json").read_text())
    assert cfg["agents"]["registered"] == ["helper"], cfg
    assert cfg["agents"]["caps"] == {"helper": 30}
    assert cfg["agents"]["builder_mode"] is True and cfg["agents"]["iter_cap"] == 30
    assert ("conversation", str(ws)) in calls, calls


def test_orchestrator_mode_registers_roles_and_workspace_agents(
        tmp_path, monkeypatch):
    """Both sources land: the AGENTS_DIR roles from preflight() AND this
    workspace's own file agents, with caps from the same budget."""
    ws = tmp_path / "proj"
    agents = ws / ".agents" / "agents"
    role_file(agents, name="ws-helper")
    local = _fake_def("ws-helper", source=str(agents / "ws-helper.md"))
    calls, registry = install_subagent_stub(monkeypatch, {str(agents): [local]})
    role = _fake_def("fixer", source="/scratch/oh_team2/agents/fixer.md")
    monkeypatch.setattr(tk, "preflight", lambda: [role])
    res = tk.run_team(brief="B", workspace=str(ws), budget_calls=5,
                      contract={"gate": "true"}, waive_lessons="unit test")
    assert {"fixer", "ws-helper"} <= set(registry._agent_factories), \
        sorted(registry._agent_factories)
    cfg = json.loads((Path(res["run_dir"]) / "config.json").read_text())
    assert sorted(cfg["agents"]["registered"]) == ["fixer", "ws-helper"], cfg
    assert cfg["agents"]["caps"] == {"fixer": 15, "ws-helper": 15}
    assert cfg["agents"]["builder_mode"] is False
    assert ("register", "general-purpose") in calls, "fixer alias still mapped"


def test_poison_in_a_run_heals_and_is_recorded(tmp_path, monkeypatch, caplog):
    """A poisoned role file no longer kills the run; config.json says what was
    repaired, so a later audit can find the file to fix."""
    ws = tmp_path / "proj"
    agents = ws / ".agents" / "agents"
    role_file(agents, name="builder", model="deepseek-v4")
    bad = _fake_def("builder", model="deepseek-v4", source=str(agents / "builder.md"))
    install_subagent_stub(monkeypatch, {str(agents): [bad]})
    with caplog.at_level(logging.WARNING, logger="teamkit"):
        res = tk.run_team(brief="B", workspace=str(ws), budget_calls=10,
                          roles="none", contract={"gate": "true"},
                          waive_lessons="unit test")
    assert res["status"] == "GATE_PASS", "healed, not refused"
    assert bad.model == "inherit"
    cfg = json.loads((Path(res["run_dir"]) / "config.json").read_text())
    assert cfg["agents"]["normalized"] == [{"name": "builder",
                                           "model": "deepseek-v4"}]
    assert "deepseek-v4" in caplog.text


def test_drift_between_two_runs_picks_up_the_edit(tmp_path, monkeypatch):
    """D7(c) #2: run 1 registers a file, the file is edited, run 2 (same
    process, same registry) executes the NEW tool list."""
    ws = tmp_path / "proj"
    agents = ws / ".openhands" / "agents"
    role_file(agents, name="worker", tools=("terminal",))
    v1 = _fake_def("worker", tools=("terminal",), source=str(agents / "worker.md"))
    install_subagent_stub(monkeypatch, {str(agents): [v1]})
    tk.run_team(brief="B", workspace=str(ws), budget_calls=10, roles="none",
                contract={"gate": "true"}, waive_lessons="unit test")
    assert list(_registry()["worker"].definition.tools) == ["terminal"]

    role_file(agents, name="worker", tools=("terminal", "file_editor"))
    v2 = _fake_def("worker", tools=("terminal", "file_editor"),
                   source=str(agents / "worker.md"))
    monkeypatch.setattr(sys.modules["openhands.sdk.subagent"],
                        "load_agents_from_dir",
                        lambda p, root=None: [v2])
    tk.run_team(brief="B", workspace=str(ws), budget_calls=10, roles="none",
                contract={"gate": "true"}, waive_lessons="unit test")
    assert list(_registry()["worker"].definition.tools) == \
        ["terminal", "file_editor"], "stale registration must not survive a run"


def test_builder_mode_zero_tool_file_is_not_rejected_loudly(tmp_path, monkeypatch):
    """Zero-tool file agents are the SDK's problem to warn about, not teamkit's
    to abort on: preflight's asserts belong to AGENTS_DIR roles only. A builder
    run with such a file still passes its gate."""
    ws = tmp_path / "proj"
    agents = ws / ".agents" / "agents"
    role_file(agents, name="hollow", tools=())
    hollow = _fake_def("hollow", tools=(), source=str(agents / "hollow.md"))
    install_subagent_stub(monkeypatch, {str(agents): [hollow]})
    res = tk.run_team(brief="B", workspace=str(ws), budget_calls=10, roles="none",
                      contract={"gate": "true"}, waive_lessons="unit test")
    assert res["status"] == "GATE_PASS"
    assert res["outcome"] == "success"


def test_refused_and_dry_runs_register_nothing(tmp_path, monkeypatch):
    """The new preflight work stays behind the D3 chokepoints: no spend path,
    no registry mutation, no calls."""
    ws = tmp_path / "proj"
    agents = ws / ".openhands" / "agents"
    role_file(agents, name="helper")
    helper = _fake_def("helper", source=str(agents / "helper.md"))
    calls, registry = install_subagent_stub(monkeypatch, {str(agents): [helper]})
    res = tk.run_team(brief="B", workspace=str(ws),
                      budget_calls=tk.BUDGET_MAX_CALLS + 1, roles="none")
    assert res["status"] == "BUDGET_REFUSED"
    assert registry._agent_factories == {} and calls == []
    monkeypatch.setenv("TEAMKIT_DRY_RUN", "1")
    res = tk.run_team(brief="B", workspace=str(ws), budget_calls=10, roles="none")
    assert res["status"] == "DRY_PLAN"
    assert registry._agent_factories == {}, "a plan must not touch the registry"
    assert calls == []


def test_preflight_no_longer_asserts_on_model_but_still_on_tools(
        tmp_path, monkeypatch, caplog):
    """preflight()'s own contract after R4b: tools still fatal (a role that
    cannot act is the incident that started this), model now self-heals."""
    agents_dir = tmp_path / "roles"
    role_file(agents_dir, name="poisoned", model="deepseek-v4")
    role_file(agents_dir, name="hollow", tools=())
    poison = _fake_def("poisoned", model="deepseek-v4",
                       source=str(agents_dir / "poisoned.md"))
    hollow = _fake_def("hollow", tools=[], source=str(agents_dir / "hollow.md"))
    calls = []
    install_oh_stubs(monkeypatch, calls)
    monkeypatch.setattr(sys.modules["openhands.sdk.subagent"],
                        "load_agents_from_dir",
                        lambda p, root=None: [poison, hollow])
    monkeypatch.setattr(sys.modules["openhands.tools.preset.default"],
                        "get_default_tools",
                        lambda **kw: [types.SimpleNamespace(tools=[
                            types.SimpleNamespace(name="terminal")])])
    monkeypatch.setattr(tk, "AGENTS_DIR", str(agents_dir))
    with caplog.at_level(logging.WARNING, logger="teamkit"):
        with pytest.raises(AssertionError, match="PREFLIGHT FAIL hollow"):
            REAL_PREFLIGHT()
    assert poison.model == "inherit", "healed before the tools assert killed it"
    assert "deepseek-v4" in caplog.text
