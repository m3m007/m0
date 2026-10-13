"""Offline tests for teamkit D1 (RunRecord) + D3 (budget chokepoint). Zero spend.

teamkit imports openhands LAZILY (inside preflight/run_team), so injecting stub
modules into sys.modules is enough to drive a whole run_team() call with zero
network and zero real API calls — which is exactly what these tests exploit.
Every "no spend" claim is asserted against a `calls` list that the stubs append
to, including the conv.run() that would be the first real request.

Nothing here talks to a endpoint, reads ~/.hermes/config.yaml (tk._key is
monkeypatched), or touches the repo's own runs/ directory (TEAMKIT_RUNS_DIR is
pointed at tmp_path).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from team import teamkit as tk  # noqa: E402

LEGACY_KEYS = {"status", "gate", "gate_tail", "lessons", "workdir_diff",
               "recent_commits", "orchestrator_final"}


# --------------------------------------------------------------- openhands stub
def install_oh_stubs(monkeypatch, calls, run_exc=None, preflight_exc=None):
    """sys.modules stubs for every lazy openhands import teamkit performs.

    Also neutralises the TDAI memory hub and the key file: a unit test must not
    read the owner's config or write shared memory.
    """
    class LLM:
        def __init__(self, **kw):
            calls.append(("llm", kw.get("model"), kw.get("base_url")))

    class _Toolset:
        tools = []

    class Agent:
        def __init__(self, **kw):
            calls.append(("agent",))

    class Conversation:
        def __init__(self, **kw):
            calls.append(("conversation", kw.get("workspace")))
            self.state = "stub-state"

        def set_confirmation_policy(self, policy):
            calls.append(("confirm",))

        def send_message(self, msg):
            calls.append(("send", len(msg)))

        def run(self):
            calls.append(("conv_run",))          # <- the real-spend point
            if run_exc:
                raise run_exc

    def get_default_tools(**kw):
        return [_Toolset()]

    sdk = types_module("openhands")
    sdk_sdk = types_module("openhands.sdk", LLM=LLM, Agent=Agent,
                           Conversation=Conversation)
    tools_pkg = types_module("openhands.tools")
    preset_pkg = types_module("openhands.tools.preset")
    preset_default = types_module("openhands.tools.preset.default",
                                  get_default_tools=get_default_tools)
    subagent = types_module("openhands.sdk.subagent",
                            load_agents_from_dir=lambda p: [],
                            register_agent_if_absent=lambda *a, **k:
                            calls.append(("register", a[0])),
                            agent_definition_to_factory=lambda d, ws: None)
    registry = types_module("openhands.sdk.subagent.registry")
    registry._agent_factories = {}
    registry._registry_lock = threading.Lock()
    security = types_module("openhands.sdk.security")
    confirmation = types_module("openhands.sdk.security.confirmation_policy",
                                NeverConfirm=lambda: "NeverConfirm")
    for name, mod in [("openhands", sdk), ("openhands.sdk", sdk_sdk),
                      ("openhands.tools", tools_pkg),
                      ("openhands.tools.preset", preset_pkg),
                      ("openhands.tools.preset.default", preset_default),
                      ("openhands.sdk.subagent", subagent),
                      ("openhands.sdk.subagent.registry", registry),
                      ("openhands.sdk.security", security),
                      ("openhands.sdk.security.confirmation_policy",
                       confirmation)]:
        monkeypatch.setitem(sys.modules, name, mod)

    def _preflight():
        calls.append(("preflight",))
        if preflight_exc:
            raise preflight_exc
        return []

    monkeypatch.setattr(tk, "preflight", _preflight)
    monkeypatch.setattr(tk, "_key", lambda: "test-key-not-a-real-one")
    monkeypatch.setattr(tk, "_tdai", lambda: None)
    return calls


def types_module(name, **attrs):
    import types
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


@pytest.fixture(autouse=True)
def runs_env(tmp_path, monkeypatch):
    """Autouse: point every run record at a throwaway dir.

    Autouse on purpose — a test that forgot the fixture would otherwise write
    real records into the repo's own runs/, which is how a suite pollutes the
    evidence trail it is supposed to be auditing.
    """
    root = tmp_path / "runs"
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(root))
    monkeypatch.delenv("TEAMKIT_DRY_RUN", raising=False)
    return root


def do_run(monkeypatch, calls, workspace, contract=None, budget_calls=10,
           **kw):
    install_oh_stubs(monkeypatch, calls)
    return tk.run_team(brief="BRIEF TEXT", workspace=str(workspace),
                       budget_calls=budget_calls, contract=contract,
                       roles="none", waive_lessons="unit test", **kw)


# ------------------------------------------------------------------ D1: records
def test_every_run_persists_config_state_summary(tmp_path, monkeypatch,
                                                 runs_env):
    ws = tmp_path / "myproj"
    ws.mkdir()
    calls: list = []
    result = do_run(monkeypatch, calls, ws, contract={"gate": "true"})

    assert result["status"] == "GATE_PASS", "legacy status must be kept"
    run_dir = Path(result["run_dir"])
    assert run_dir.parent == runs_env
    assert (run_dir / "config.json").is_file()
    assert (run_dir / "state.json").is_file()
    assert (run_dir / "summary.json").is_file()

    cfg = json.loads((run_dir / "config.json").read_text())
    assert cfg["workspace"] == str(ws)
    assert cfg["model"] == "openai/qwen3.8-flash"
    assert cfg["base_url"] == tk.TP_COMPAT
    assert cfg["budget_calls"] == 10
    assert cfg["contract"] == {"gate": "true"}, "contract recorded verbatim"
    assert cfg["roles"] == "none"
    import hashlib
    assert cfg["brief_sha256"] == hashlib.sha256(b"BRIEF TEXT").hexdigest()
    assert "brief" not in cfg and "spec" not in cfg, "text must never be stored"

    st = json.loads((run_dir / "state.json").read_text())
    assert set(st) == set(tk.STAGES) == {"preflight", "team_run", "gate",
                                         "lessons"}
    assert all(st[s]["status"] == "done" for s in tk.STAGES), st
    assert all(st[s]["attempts"] == 1 for s in tk.STAGES), st

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary == result, "summary.json is exactly the returned dict + run_dir"
    assert summary["run_dir"] == str(run_dir)


def test_return_dict_stays_byte_compatible_plus_run_dir(tmp_path, monkeypatch,
                                                        runs_env):
    """Old callers see the old keys with the old meaning; only run_dir/pair added."""
    ws = tmp_path / "myproj"
    ws.mkdir()
    result = do_run(monkeypatch, [], ws, contract={"gate": "true"})
    assert LEGACY_KEYS <= set(result)
    assert set(result) == LEGACY_KEYS | {"run_dir", "pair"}
    assert result["pair"] is None, "legacy single gate reports no pair check"
    assert result["gate"] == "true"
    assert result["lessons"] == "WAIVED: unit test"


def test_run_dir_slug_is_utc_timestamp_plus_24_safe_chars(tmp_path, monkeypatch,
                                                          runs_env):
    ws = tmp_path / "weird!! name/that.is_quite-long-and-over-24"
    ws.mkdir(parents=True)
    result = do_run(monkeypatch, [], ws)
    name = Path(result["run_dir"]).name
    m = re.fullmatch(r"(\d{8}T\d{6}Z)-(.{1,24})", name)
    assert m, name
    slug = m.group(2)
    assert slug == tk.run_slug(str(ws))
    assert len(slug) <= 24 and re.fullmatch(r"[A-Za-z0-9._-]+", slug)


def test_recorder_crash_returns_normal_dict_with_run_dir_none(tmp_path,
                                                             monkeypatch,
                                                             runs_env):
    """A broken recorder may not crash a run nor change what it returns."""
    ws = tmp_path / "myproj"
    ws.mkdir()
    calls: list = []
    result = do_run(monkeypatch, calls, ws, contract={"gate": "true"})

    def boom(self, name, obj):
        raise RuntimeError("recorder is on fire")

    monkeypatch.setattr(tk.RunRecorder, "write_json", boom)
    broken = do_run(monkeypatch, calls, ws, contract={"gate": "true"})

    assert broken["run_dir"] is None
    for key in LEGACY_KEYS:
        assert broken[key] == result[key], f"{key} changed when recording broke"
    assert broken["status"] == "GATE_PASS"


def test_live_lock_refuses_second_runner_before_any_api_spend(tmp_path,
                                                              monkeypatch,
                                                              runs_env):
    pid = spawn_live_pid()
    try:
        lock_dir = runs_env / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        slug = tk.run_slug(str(tmp_path / "locked-ws"))
        (lock_dir / f"{slug}.lock").write_text(str(pid))
        ws = tmp_path / "locked-ws"
        ws.mkdir()
        calls: list = []
        with pytest.raises(tk.FileLockError, match="locked by live pid"):
            do_run(monkeypatch, calls, ws, contract={"gate": "true"})
        assert calls == [], "refusal must happen before preflight/LLM/conv.run()"
        assert not any(c[0] == "conv_run" for c in calls)
        assert list(runs_env.iterdir()) == [lock_dir]
    finally:
        terminate(pid)


def test_stale_lock_from_a_dead_pid_is_reclaimed(tmp_path, monkeypatch,
                                                 runs_env):
    ws = tmp_path / "stale-ws"
    ws.mkdir()
    dead = spawn_dead_pid()
    lock_dir = runs_env / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / f"{tk.run_slug(str(ws))}.lock"
    lock.write_text(str(dead))
    calls: list = []
    result = do_run(monkeypatch, calls, ws)
    assert result["status"] == "RUN_DONE_NO_GATE"
    assert not lock.exists(), "run releases its own lock"
    assert Path(result["run_dir"]).is_dir()


def test_stage_that_raises_is_recorded_and_record_still_written(tmp_path,
                                                                monkeypatch,
                                                                runs_env):
    ws = tmp_path / "myproj"
    ws.mkdir()
    calls: list = []
    # roles != 'none' is what actually reaches preflight() (roles='none' skips it)
    install_oh_stubs(monkeypatch, calls,
                     preflight_exc=ValueError("PREFLIGHT FAIL fixer: unregistered"))
    with pytest.raises(ValueError, match="PREFLIGHT FAIL"):
        tk.run_team(brief="b", workspace=str(ws), budget_calls=5,
                    waive_lessons="unit test")
    run_dirs = [p for p in runs_env.iterdir() if p.is_dir() and p.name != ".locks"]
    assert len(run_dirs) == 1
    st = json.loads((run_dirs[0] / "state.json").read_text())
    assert st["preflight"]["status"] == "fail"
    assert st["preflight"]["attempts"] == 1
    assert st["preflight"]["error"].startswith("ValueError:")
    assert "PREFLIGHT FAIL" in st["preflight"]["error"]
    # the record survives: config written before the stage ran
    assert (run_dirs[0] / "config.json").is_file()
    summary = json.loads((run_dirs[0] / "summary.json").read_text())
    assert summary["status"] == "ERROR" and "ValueError" in summary["error"]
    assert not (runs_env / ".locks" /
                f"{tk.run_slug(str(ws))}.lock").exists(), "lock released"


def test_conv_run_failure_recorded_on_team_run_stage(tmp_path, monkeypatch,
                                                     runs_env):
    ws = tmp_path / "myproj"
    ws.mkdir()
    calls: list = []
    install_oh_stubs(monkeypatch, calls, run_exc=RuntimeError("stuck loop"))
    with pytest.raises(RuntimeError, match="stuck loop"):
        tk.run_team(brief="b", workspace=str(ws), budget_calls=5, roles="none",
                    waive_lessons="unit test")
    assert not any(c[0] == "gate" for c in calls)
    run_dir = next(p for p in runs_env.iterdir() if p.is_dir() and p.name != ".locks")
    st = json.loads((run_dir / "state.json").read_text())
    assert st["preflight"]["status"] == "done"
    assert st["team_run"]["status"] == "fail"
    assert st["team_run"]["error"].startswith("RuntimeError: stuck loop")
    assert st["gate"]["status"] == "pending"
    assert ("conv_run",) in calls, "run() was reached, then raised"


def test_gate_stage_records_failure_with_output_tail(tmp_path, monkeypatch,
                                                     runs_env):
    ws = tmp_path / "myproj"
    ws.mkdir()
    calls: list = []
    result = do_run(monkeypatch, calls, ws, contract={"gate": "exit 3"})
    assert result["status"] == "GATE_FAIL"
    st = json.loads((Path(result["run_dir"]) / "state.json").read_text())
    assert st["gate"]["status"] == "fail" and st["gate"]["attempts"] == 1
    assert st["gate"]["error"].startswith("RuntimeError:")


# ---------------------------------------------------------------- D3: chokepoint
def test_budget_above_120_refused_with_zero_side_effects(tmp_path, monkeypatch):
    runs_env = tmp_path / "runs"
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(runs_env))
    calls: list = []
    install_oh_stubs(monkeypatch, calls)
    res = tk.run_team(brief="b", workspace=str(tmp_path), budget_calls=121,
                      contract={"gate": "true"}, roles="none",
                      waive_lessons="unit test")
    assert res["status"] == "BUDGET_REFUSED"
    assert LEGACY_KEYS <= set(res)
    assert res["gate"] is None and res["gate_tail"] == "" and res["run_dir"] is None
    assert res["pair"] is None
    assert calls == [], "refusal must not reach preflight, LLM or Conversation"
    assert not runs_env.exists(), "refusal must not create a run dir"


def test_budget_at_the_ceiling_is_allowed_above_it_is_not(tmp_path, monkeypatch,
                                                           runs_env):
    """The chokepoint is >120, not >=120: an off-by-one here refuses real runs."""
    ws = tmp_path / "myproj"
    ws.mkdir()
    ok = do_run(monkeypatch, [], ws, budget_calls=tk.BUDGET_MAX_CALLS)
    assert ok["status"] != "BUDGET_REFUSED", "120 must be allowed through"
    assert Path(ok["run_dir"]).is_dir()
    calls: list = []
    install_oh_stubs(monkeypatch, calls)
    refused = tk.run_team(brief="b", workspace=str(ws),
                          budget_calls=tk.BUDGET_MAX_CALLS + 1, roles="none")
    assert refused["status"] == "BUDGET_REFUSED"
    assert calls == []


def test_dry_run_env_records_plan_and_makes_zero_calls(tmp_path, monkeypatch,
                                                       runs_env, capsys):
    ws = tmp_path / "dryproj"
    ws.mkdir()
    calls: list = []
    install_oh_stubs(monkeypatch, calls)
    monkeypatch.setenv("TEAMKIT_DRY_RUN", "1")
    res = tk.run_team(brief="plan me", workspace=str(ws), budget_calls=9,
                      contract={"gate": "pytest -q", "incumbent_gate": "old"},
                      roles="none", waive_lessons="unit test")
    assert res["status"] == "DRY_PLAN"
    assert calls == [], "TEAMKIT_DRY_RUN must not touch preflight/LLM/conv"
    run_dir = Path(res["run_dir"])
    assert (run_dir / "config.json").is_file() and (run_dir / "state.json").is_file()
    assert not (run_dir / "summary.json").exists(), "a plan is not a completed run"
    cfg = json.loads((run_dir / "config.json").read_text())
    assert cfg["base_url"] == tk.TP_COMPAT and cfg["model"] == "openai/qwen3.8-flash"
    assert cfg["contract"] == {"gate": "pytest -q", "incumbent_gate": "old"}
    out = capsys.readouterr().out
    assert "DRY_PLAN" in out and "pytest -q" in out and str(ws) in out
    assert tk.TP_COMPAT in out, "plan prints base_url after lane_check"
    for stage in tk.STAGES:
        assert stage in out
    # and a dry run releases its lock, so a real run may follow immediately
    calls2: list = []
    monkeypatch.delenv("TEAMKIT_DRY_RUN")
    real = do_run(monkeypatch, calls2, ws)
    assert real["status"] == "RUN_DONE_NO_GATE" and real["run_dir"]


def test_dry_run_budget_guard_precedes_plan(tmp_path, monkeypatch):
    runs_env = tmp_path / "runs"
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(runs_env))
    monkeypatch.setenv("TEAMKIT_DRY_RUN", "1")
    res = tk.run_team(budget_calls=999, workspace=str(tmp_path))
    assert res["status"] == "BUDGET_REFUSED"
    assert not runs_env.exists()


def test_two_runs_never_share_one_record_dir(tmp_path, monkeypatch, runs_env):
    """Same slug, same UTC second: the second record must not clobber the first."""
    ws = tmp_path / "myproj"
    ws.mkdir()
    first = do_run(monkeypatch, [], ws)
    second = do_run(monkeypatch, [], ws)
    assert first["run_dir"] != second["run_dir"]
    a = json.loads((Path(first["run_dir"]) / "summary.json").read_text())
    b = json.loads((Path(second["run_dir"]) / "summary.json").read_text())
    assert a["run_dir"] == first["run_dir"] and b["run_dir"] == second["run_dir"]


HOLDER_SRC = """
import sys, os, time
sys.path.insert(0, %r)
os.environ['TEAMKIT_RUNS_DIR'] = %r
from team import teamkit as tk
run_dir, lock = tk.acquire_run_lock(%r, os.environ['TEAMKIT_RUNS_DIR'])
print('LOCKED', lock, flush=True)
time.sleep(60)
"""


def test_a_second_process_holding_the_slug_lock_is_refused_before_spend(
        tmp_path, monkeypatch, runs_env):
    """Real cross-process refusal: another pid owns the slug lock, so run_team
    raises FileLockError without ever reaching the stubbed LLM/Conversation."""
    ws = tmp_path / "shared-ws"
    ws.mkdir()
    holder = subprocess.Popen(
        [sys.executable, "-c",
         HOLDER_SRC % (str(REPO), str(runs_env), tk.run_slug(str(ws)))],
        stdout=subprocess.PIPE, text=True)
    try:
        line = holder.stdout.readline(60)          # blocks until the lock is ours
        assert line.startswith("LOCKED"), line
        calls: list = []
        with pytest.raises(tk.FileLockError, match="locked by live pid"):
            do_run(monkeypatch, calls, ws)
        assert calls == [], "no preflight, no LLM, no conv.run() on refusal"
    finally:
        holder.terminate()
        holder.wait(timeout=30)


# ------------------------------------------------------------------- tiny helpers
def spawn_live_pid():
    """A real, live, unrelated child process to own a lock file."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    return proc.pid


def terminate(pid):
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def spawn_dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid
