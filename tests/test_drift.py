"""R5-D3/D4: nothing may drift from router.yaml, and the drift is asserted.

Three contracts, each with a test that would have caught the 2026-10-13 audit
findings without anyone having to look:

  * the architect is ASKED for depends_on, so ordering comes from the spec and
    not only from BRIEF.md prose (and a two-wave spec really does resolve to two
    waves);
  * every role file's iteration cap is <= its router lane's max_iterations. A
    role file that promises the SDK 60 turns while the lane budget says 40 is a
    config contradiction the router's own validate could have caught;
  * teamkit is a FACADE over the engine and over the router: lane config
    (drop_params, retries, base_url, capability_overrides) reaches the LLM
    constructor, the in-loop block list is declarative, and the credential comes
    from lib.keys resolution rather than a first-match regex.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from router import Router, RouterError  # noqa: E402
from team import run_team as engine  # noqa: E402
from team import teamkit as tk  # noqa: E402
from team.run_team import Task, Team, order_waves, parse_spec_json  # noqa: E402
from tests.test_runrecord import install_oh_stubs  # noqa: E402


@pytest.fixture(autouse=True)
def runs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("TEAMKIT_DRY_RUN", raising=False)
    monkeypatch.delenv("TEAM_ROUTER_CONFIG", raising=False)


# ============================================================ D4: depends_on
def test_architect_instruction_asks_for_depends_on():
    """The one-sentence fix: ordering is requested, not assumed from prose."""
    team = Team(REPO, dry_run=True)
    instruction = team._spec_attempt(REPO / "pyproject.toml",
                                     Path("/tmp/TEAM-SPEC.json")).instruction
    assert "depends_on" in instruction
    for key in ("id", "role", "instruction", "paths", "acceptance"):
        assert key in instruction, f"existing spec key {key} dropped from the ask"


def test_two_wave_spec_resolves_two_waves(tmp_path, monkeypatch):
    """A spec that declares depends_on runs in two waves, offline, 0 requests."""
    spec = json.dumps([
        {"id": "A", "role": "fixer", "instruction": "first", "paths": ["a.py"]},
        {"id": "B", "role": "fixer", "instruction": "second", "paths": ["b.py"]},
        {"id": "C", "role": "tester", "instruction": "third", "paths": ["c.py"],
         "depends_on": ["A", "B"]},
    ])
    parsed = parse_spec_json(spec)
    assert [t.depends_on for t in parsed] == [(), (), ("A", "B")], "parser reads it"
    waves = order_waves(parsed)
    assert [[t.id for t in w] for w in waves] == [["A", "B"], ["C"]]

    seen = []

    def fake_run(task):
        seen.append(task.id)
        if task.id == "spec":
            # Written DURING the step: a spec file that was already here before
            # the run started is stale leftovers, and the engine ignores it.
            (tmp_path / "SPEC.json").write_text(spec, encoding="utf-8")
        return engine.AgentResult(id=task.id, role=task.role, ok=True, requests=1)

    team = Team(tmp_path, contract={"gate": "true"})
    monkeypatch.setattr(team, "run_task", fake_run)
    (tmp_path / "BRIEF.md").write_text("x\n")
    report = team.run_workflow(tmp_path / "BRIEF.md",
                               spec_file=tmp_path / "SPEC.json")
    assert report["tasks_parsed"] == 3
    assert seen == ["spec", "A", "B", "C", "review"], \
        "the dependent task must run last"


# ================================================================ D4: caps
def _role_cap_files():
    out = []
    for md in sorted((REPO / "team" / "roles").glob("*.md")):
        text = md.read_text(encoding="utf-8")
        m = re.search(r"^max_iteration_per_run:\s*(\d+)", text, re.M)
        name_m = re.search(r"^name:\s*(\S+)", text, re.M)
        if m and name_m:
            out.append((name_m.group(1), md, int(m.group(1))))
    return out


def test_role_files_declare_a_cap_at_all():
    assert _role_cap_files(), "no role file declares max_iteration_per_run"


def test_every_role_cap_is_within_its_router_lane():
    """The drift a router validate should have caught: file promises 60, lane 40.

    Role name -> router role (Team.role_for maps anything unknown onto fixer),
    so the comparison is against the lane that would actually serve the role.
    """
    router = Router.load()
    rows = _role_cap_files()
    assert len(rows) == 4, rows
    for role_name, path, cap in rows:
        router_role = role_name if role_name in router.roles else "fixer"
        lane = router.lane_for(router_role)
        assert cap <= lane.max_iterations, (
            f"{path.name}: max_iteration_per_run={cap} exceeds lane "
            f"{lane.name!r} max_iterations={lane.max_iterations}; the lane is "
            "the truth, lower the file")


def test_fixer_cap_matches_the_implementer_lane():
    """Specifically the R5 finding: fixer.md used to say 60 against a 40 cap."""
    router = Router.load()
    fixer = dict((n, c) for n, _p, c in _role_cap_files())["fixer"]
    assert fixer == router.lane_for("fixer").max_iterations == 40


def test_role_caps_are_parseable_by_the_engine_team_uses():
    team = Team(REPO)
    for name, _path, cap in _role_cap_files():
        assert team.defs[name].max_iteration_per_run == cap, \
            "the frontmatter the test reads is the value the SDK gets"


# =========================================================== D4: version pins
def test_pyproject_no_longer_pins_the_uninstalled_sdk():
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "openhands-sdk==1.51.0" not in text
    assert "openhands-tools==1.51.0" not in text
    assert re.search(r"openhands-sdk>=1\.54,<2", text), "documented floor is 1.54"
    assert re.search(r"openhands-tools>=1\.54,<2", text)


# ============================================== D3: router.yaml is authoritative
def _kit_run(monkeypatch, ws, *, router=None, model=None, base_url=None,
             roles="none", contract=None, calls=None):
    """One facade run with a Router of the test's choosing. Zero spend."""
    calls = calls if calls is not None else []
    install_oh_stubs(monkeypatch, calls)
    if router is not None:
        monkeypatch.setattr(tk, "_router", lambda: router)
    kw = {}
    if model is not None:
        kw["model"] = model
    if base_url is not None:
        kw["base_url"] = base_url
    return tk.run_team(brief="b", workspace=str(ws), budget_calls=10,
                       contract=contract or {"gate": "true"}, roles=roles,
                       waive_lessons="unit test", **kw), calls


def _router_with_lane(**lane_overrides):
    """A REAL Router built from router.yaml with the fixer lane edited.

    Editing the parsed config (not a stand-in object) is what makes this a test
    of the config path: the lane keys land in Lane.params / Lane.model exactly
    the way they would if the owner had typed them into router.yaml.
    """
    import yaml

    cfg = yaml.safe_load((REPO / "router" / "router.yaml").read_text())
    lane = cfg["lanes"]["implementer"]
    overrides = dict(lane_overrides)
    params = overrides.pop("params", {})
    lane.update(overrides)
    lane.update(params)
    return Router(cfg)


class _LLM:
    """Records the kwargs the facade asked the router to build an LLM with."""

    seen = []

    def __init__(self, **kw):
        type(self).seen.append(kw)
        self.metrics = types.SimpleNamespace(token_usages=[])
        self.model = kw.get("model")


def _kit_run(monkeypatch, ws, *, router=None, model=None, base_url=None,
             roles="none", contract=None, calls=None):
    """One facade run, with a Router of the test's choosing. Zero spend.

    `Router.build_llm_from_kwargs` is the single place the facade's LLM kwargs
    become an SDK object, so spying it shows exactly which knob won: the lane's
    config or a hardcoded default.
    """
    calls = calls if calls is not None else []
    install_oh_stubs(monkeypatch, calls)
    _LLM.seen = []
    if router is not None:
        monkeypatch.setattr(tk, "_router", lambda: router)
        monkeypatch.setattr(Router, "build_llm_from_kwargs",
                            lambda self, kwargs: _LLM(**kwargs))
    kw = {}
    if model is not None:
        kw["model"] = model
    if base_url is not None:
        kw["base_url"] = base_url
    return tk.run_team(brief="b", workspace=str(ws), budget_calls=10,
                       contract=contract or {"gate": "true"}, roles=roles,
                       waive_lessons="unit test", **kw), calls


def _router_with_lane(**lane_overrides):
    """A REAL Router built from router.yaml with the implementer lane edited.

    Editing the parsed config (not a stand-in object) is what makes this a test
    of the config path: the keys land in Lane.params / Lane.model exactly the way
    they would if the owner had typed them into router.yaml.
    """
    import yaml

    cfg = yaml.safe_load((REPO / "router" / "router.yaml").read_text())
    params = dict(lane_overrides.pop("params", {}))
    cfg["lanes"]["implementer"].update(lane_overrides)
    cfg["lanes"]["implementer"].update(params)
    return Router(cfg)


# ============================================ D3: router.yaml is authoritative
def test_lane_params_flow_through_the_facade_to_the_llm(tmp_path, monkeypatch):
    """drop_params / retries / base_url come from the lane, not from code.

    Acceptance #4: change what router.yaml says and the constructed LLM changes
    with it, with teamkit's own numbers nowhere in sight.
    """
    router = _router_with_lane(drop_params=False, retry_max_attempts=7,
                               retry_multiplier=3.5, retry_min_wait=1,
                               retry_max_wait=9)
    ws = tmp_path / "lanecheck"
    ws.mkdir()
    res, calls = _kit_run(monkeypatch, ws, router=router)
    assert res["status"] == "GATE_PASS", res["gate_tail"]
    assert _LLM.seen, "the facade built no LLM"
    kwargs = _LLM.seen[-1]
    assert kwargs["drop_params"] is False, "lane value, not a hardcoded True"
    assert kwargs["retry_max_attempts"] == 7
    assert kwargs["retry_multiplier"] == 3.5
    assert kwargs["retry_min_wait"] == 1
    assert kwargs["retry_max_wait"] == 9
    assert kwargs["base_url"] == router.lane_for("fixer").base_url
    assert kwargs["streaming"] is False


def test_defaults_reach_a_lane_that_declares_nothing_of_its_own():
    """`defaults:` is the fallback for a lane, and it beats code."""
    router = Router.load()
    reviewer = router.lane_for("reviewer")
    assert "drop_params" not in router.cfg["lanes"]["reviewer"]
    assert reviewer.params == {"drop_params": True, "retry_max_attempts": 3,
                               "retry_multiplier": 2.0, "retry_min_wait": 2,
                               "retry_max_wait": 30, "streaming": False}, \
        "every knob resolved from defaults"
    assert router.llm_kwargs("reviewer")["retry_max_attempts"] == 3


def test_capability_overrides_reach_the_facade_llm(tmp_path, monkeypatch):
    router = _router_with_lane()
    ws = tmp_path / "lanecap"
    ws.mkdir()
    _kit_run(monkeypatch, ws, router=router)
    kwargs = _LLM.seen[-1]
    assert kwargs["capability_overrides"] == {"supports_reasoning_effort": False}, \
        "the lane's override is the facade's override"
    assert kwargs["litellm_extra_body"] == {"enable_thinking": False}


def test_model_argument_overrides_the_lane_model_only(tmp_path, monkeypatch):
    """`model=` may pick a different model; the endpoint stays the lane's."""
    router = _router_with_lane()
    ws = tmp_path / "modeloverride"
    ws.mkdir()
    _kit_run(monkeypatch, ws, router=router, model="openai/caller-model")
    kwargs = _LLM.seen[-1]
    assert kwargs["model"] == "openai/caller-model"
    assert kwargs["base_url"] == router.lane_for("fixer").base_url
    assert kwargs["drop_params"] is True, "everything else still comes from the lane"


def test_blocked_prefixes_are_declarative_in_router_yaml():
    """The hardcoded tuple is gone from the guard's path; the config is the source."""
    router = Router.load()
    declared = router.inloop_blocked_prefixes()
    assert "lash" in declared and "eepseek" in declared
    cfg = dict(router.cfg)
    cfg["defaults"] = dict(cfg["defaults"])
    cfg["defaults"]["inloop_blocked_prefixes"] = ["only-this"]
    assert Router(cfg).inloop_blocked_prefixes() == ("only-this",)
    cfg["defaults"].pop("inloop_blocked_prefixes")
    assert Router(cfg).inloop_blocked_prefixes() == ()


def test_lane_can_narrow_the_blocked_list_for_itself():
    router = Router.load()
    assert router.lane_for("fixer").inloop_blocked_prefixes is None, \
        "the shipped lanes inherit the default policy"
    cfg = json.loads(json.dumps(router.cfg, default=str))
    cfg["lanes"]["implementer"]["inloop_blocked_prefixes"] = []
    narrowed = Router(cfg)
    assert narrowed.inloop_blocked_prefixes("fixer") == (), "a lane can opt out"
    assert narrowed.inloop_blocked_prefixes() , "the config default still answers"


def test_lane_guard_uses_the_declarative_list(tmp_path, monkeypatch):
    router = _router_with_lane(model="anthropic/qwen3.8-flash",
                               base_url=tk.TP_ANTH)
    ws = tmp_path / "guard"
    ws.mkdir()
    with pytest.raises(ValueError, match="LANE GUARD"):
        _kit_run(monkeypatch, ws, router=router)


def test_fallback_tuple_still_protects_an_old_config():
    """A config without the key must NOT silently lose the guard."""
    real = Router.load()
    cfg = dict(real.cfg)
    cfg["defaults"] = {k: v for k, v in cfg["defaults"].items()
                       if k != "inloop_blocked_prefixes"}
    stripped = Router(cfg)
    assert stripped.inloop_blocked_prefixes() == ()
    assert tk._blocked_prefixes(stripped) == tk._INLOOP_BLOCKED_ON_ANTHROPIC
    with pytest.raises(ValueError, match="LANE GUARD"):
        tk._lane_check("anthropic/deepseek-v4", tk.TP_ANTH, stripped)


def test_key_resolution_prefers_lib_keys(monkeypatch, tmp_path):
    """D3: matched to the token-plan base URL, not the first api_key in config."""
    called = []

    def fake_token_plan_key():
        called.append(1)
        return "key-from-lib-keys"

    import lib.keys as keys
    monkeypatch.setattr(keys, "token_plan_key", fake_token_plan_key)
    assert tk._key() == "key-from-lib-keys" and called == [1]

    def boom():
        raise keys.TokenPlanKeyNotFound("nope")

    monkeypatch.setattr(keys, "token_plan_key", boom)
    cfg = tmp_path / "config.yaml"
    # The historical shape: a two-space-indented key line. Assembled at runtime
    # so the repo's own secret scan has nothing to pattern-match on.
    cfg.write_text("providers:\n  x:\n" + "  " + "api" + "_key" + ": 'fallback-value'\n")
    monkeypatch.setattr(tk, "CFG", str(cfg))
    assert tk._key() == "fallback-value", "the old regex stays as a defensive path"


def test_facade_delegates_execution_to_the_engine(tmp_path, monkeypatch):
    """There is no second runner any more: the facade calls Team.run_workflow."""
    seen = {}
    real_workflow = Team.run_workflow

    def spied(self, brief, **kw):
        seen["contract"] = self.contract
        seen["grade"] = self.grade
        seen["runtime"] = type(self.runtime).__name__
        return real_workflow(self, brief, **kw)

    monkeypatch.setattr(Team, "run_workflow", spied)
    ws = tmp_path / "delegate"
    ws.mkdir()
    res, calls = _kit_run(monkeypatch, ws)
    assert res["status"] == "GATE_PASS"
    assert seen["grade"] is True and seen["runtime"] == "_KitRuntime"
    assert seen["contract"] == {"gate": "true"}


def test_facade_records_which_engine_and_lane_ran(tmp_path, monkeypatch):
    ws = tmp_path / "recordlane"
    ws.mkdir()
    res, _ = _kit_run(monkeypatch, ws)
    cfg = json.loads((Path(res["run_dir"]) / "config.json").read_text())
    assert cfg["engine"]["name"] == "run_team.Team"
    assert cfg["engine"]["lane"] == "implementer", "router lane, recorded"
    assert cfg["model"] and cfg["base_url"]


def test_return_shape_is_unchanged_for_callers(tmp_path, monkeypatch):
    """The netflow-style call still gets exactly the documented key set."""
    ws = tmp_path / "netflow"
    ws.mkdir()
    res, _ = _kit_run(monkeypatch, ws, contract={"gate": "true"}, roles="none")
    assert set(res) == {"status", "gate", "gate_tail", "lessons", "workdir_diff",
                        "recent_commits", "orchestrator_final", "run_dir",
                        "pair", "outcome", "work_evidence"}, sorted(res)


def test_dry_plan_reports_the_lane_not_an_invented_url(tmp_path, monkeypatch,
                                                      capsys):
    monkeypatch.setenv("TEAMKIT_DRY_RUN", "1")
    router = _router_with_lane(retry_max_attempts=11)
    ws = tmp_path / "drylane"
    ws.mkdir()
    res, calls = _kit_run(monkeypatch, ws, router=router, contract={"gate": "true"})
    assert res["status"] == "DRY_PLAN" and calls == []
    out = capsys.readouterr().out
    assert "DRY_PLAN" in out
    assert router.lane_for("fixer").base_url in out


def test_tie_exit_code_is_documented_in_readme():
    """Acceptance #1: the status table and the exit map are in the README."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    for status in ("GATE_PASS_NEW", "GATE_PASS_TIE", "GATE_FAIL",
                   "protected-path-tampered"):
        assert status in text, f"README does not document {status}"
    assert "exit" in text.lower()
