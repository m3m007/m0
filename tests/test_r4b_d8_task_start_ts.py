"""R4b-D8: run_team() exports $TASK_START_TS, and the gate subprocess can see it.

team/roles/tester.md windows its ledger delta with `TASK_START_TS`: spend since
the start of THIS run, versus the session cumulative. Until R4b nothing set the
variable, so the two numbers were always identical and the role's own text had to
admit it ("TASK_START_TS unset: window is the whole session"). The one-liner's
tests in tests/test_tdai_bridge.py already cover both branches; what was missing
is the producer.

Proven here, offline:
  * the gate command — a real subprocess — sees a non-empty $TASK_START_TS,
    which is the brief's own acceptance probe (`test -n "$TASK_START_TS"`);
  * the exported value is the run-start epoch, integer seconds, and it is not
    stale: a value left in the environment by an earlier run or by the caller's
    shell is REPLACED, because a window that starts before the run inflates
    spent_since_start;
  * config.json records the same number, so a finished run can be audited for
    the window its children were given;
  * it is exported on the pre-spend path (before the LLM/Conversation exist),
    so the agent's own terminal tool inherits it, not just the gate;
  * both modes do it: builder (`roles='none'`) and orchestrator.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from team import teamkit as tk  # noqa: E402
from tests.test_runrecord import install_oh_stubs  # noqa: E402


@pytest.fixture(autouse=True)
def runs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("TEAMKIT_DRY_RUN", raising=False)
    monkeypatch.delenv("TASK_START_TS", raising=False)


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "tsrepo"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", str(ws)], check=False)
    return ws


def read_json(res, name):
    return json.loads((Path(res["run_dir"]) / name).read_text())


def test_gate_subprocess_sees_task_start_ts(repo, monkeypatch):
    """The brief's acceptance probe, verbatim: a gate stub that only passes when
    the variable is set and non-empty."""
    calls = []
    install_oh_stubs(monkeypatch, calls)
    res = tk.run_team(brief="B", workspace=str(repo), budget_calls=10,
                      roles="none", contract={"gate": 'test -n "$TASK_START_TS"'},
                      waive_lessons="unit test")
    assert res["status"] == "GATE_PASS", res["gate_tail"]
    assert res["outcome"] == "success"


def test_value_is_run_start_epoch_and_replaces_a_stale_one(repo, monkeypatch):
    """A leftover TASK_START_TS from the caller's shell is not a window: the run
    sets its own start, and the recorded value is what the child received."""
    monkeypatch.setenv("TASK_START_TS", str(int(time.time()) - 86_400))
    calls = []
    install_oh_stubs(monkeypatch, calls)
    before = int(time.time())
    res = tk.run_team(brief="B", workspace=str(repo), budget_calls=10,
                      roles="none",
                      contract={"gate": 'python3 -c '
                                        '"import os;print(os.environ[\'TASK_START_TS\'])"'},
                      waive_lessons="unit test")
    assert res["status"] == "GATE_PASS", res["gate_tail"]
    seen = int(res["gate_tail"].strip().splitlines()[-1])
    cfg = read_json(res, "config.json")
    assert seen >= before, f"gate saw a stale window: {seen} < {before}"
    assert seen <= int(time.time()) + 1, seen
    assert cfg["task_start_ts"] == seen, (cfg, seen)
    assert os.environ["TASK_START_TS"] == str(seen), "the export is process-wide"


def test_exported_before_any_conversation_exists(repo, monkeypatch):
    """Order matters: the agent's terminal tool inherits the environment of the
    process that builds the Conversation, so the export must precede the spend
    path, not merely happen somewhere in the run.

    Probed with `tk._key`, which the run calls exactly once, on the LLM
    construction line — the first pre-spend step after preflight. Whatever it
    observes in os.environ is what the child processes will inherit.
    """
    seen_at_key = []
    calls = []
    install_oh_stubs(monkeypatch, calls)
    before = int(time.time())

    def probing_key():
        seen_at_key.append(os.environ.get("TASK_START_TS"))
        return "test-key-not-a-real-one"

    monkeypatch.setattr(tk, "_key", probing_key)
    res = tk.run_team(brief="B", workspace=str(repo), budget_calls=10,
                      roles="none", contract={"gate": "true"},
                      waive_lessons="unit test")
    cfg = read_json(res, "config.json")
    assert seen_at_key and seen_at_key[0] == str(cfg["task_start_ts"]), seen_at_key
    assert int(seen_at_key[0]) >= before, seen_at_key
    assert ("llm", "openai/qwen3.8-flash", tk.TP_COMPAT) in calls, calls
    assert ("conversation", str(repo)) in calls, calls
    assert os.environ["TASK_START_TS"] == str(cfg["task_start_ts"])


def test_orchestrator_mode_exports_it_too(repo, monkeypatch):
    """roles != 'none' reaches preflight(); the export is not builder-only."""
    calls = []
    install_oh_stubs(monkeypatch, calls)
    res = tk.run_team(brief="B", workspace=str(repo), budget_calls=10,
                      contract={"gate": 'test -n "$TASK_START_TS"'},
                      waive_lessons="unit test")
    assert res["status"] == "GATE_PASS", res["gate_tail"]
    assert ("preflight",) in calls, calls
    cfg = read_json(res, "config.json")
    assert isinstance(cfg["task_start_ts"], int) and cfg["task_start_ts"] > 0


def test_readme_documents_the_window(tmp_path):
    """D8 asks for a README note; a documented-but-unmentioned contract is the
    kind of gap the audit keeps finding, so the line is asserted, not trusted."""
    text = (REPO / "README.md").read_text()
    assert "TASK_START_TS" in text, "README must name the variable it exports"
    assert "recall" in text, "D6: the record's recall key is documented too"


def test_refused_and_dry_runs_do_not_touch_the_environment(repo, monkeypatch):
    """D3 chokepoints come first: a run that refuses must not rewrite the
    caller's environment (the same no-side-effect promise as the run dir)."""
    monkeypatch.setenv("TASK_START_TS", "111")
    calls = []
    install_oh_stubs(monkeypatch, calls)
    res = tk.run_team(brief="B", workspace=str(repo),
                      budget_calls=tk.BUDGET_MAX_CALLS + 1, roles="none")
    assert res["status"] == "BUDGET_REFUSED"
    assert os.environ["TASK_START_TS"] == "111", "refusal changed the env"
    monkeypatch.setenv("TEAMKIT_DRY_RUN", "1")
    res = tk.run_team(brief="B", workspace=str(repo), budget_calls=10,
                      roles="none")
    assert res["status"] == "DRY_PLAN"
    assert os.environ["TASK_START_TS"] == "111", "a plan must not set the window"
    assert calls == []


def test_tester_role_one_liner_windows_on_the_exported_value(repo, monkeypatch,
                                                             tmp_path):
    """The consumer, end to end: what teamkit exports IS what
    team/roles/tester.md's budget one-liner reads, and the window is real —
    pre-existing rows stop counting, rows written after the export count.

    Two scripts, run as the gate: one appends a row (the spend the tester would
    be measuring), one is the role's own windowing logic over that ledger.
    """
    ledger = tmp_path / "calls.jsonl"
    now = time.time()
    ledger.write_text("\n".join(json.dumps(r) for r in [
        {"ts": now - 4000, "source": "multiagent-stack:router:fixer", "calls": 10},
        {"ts": now - 3000, "source": "multiagent-stack:router:tester", "calls": 4},
    ]) + "\n")
    spend = tmp_path / "spend.py"
    spend.write_text("import json,os,time\n"
                     "p=os.environ['MULTIAGENT_LEDGER_PATH']\n"
                     "assert int(os.environ['TASK_START_TS']) <= time.time()\n"
                     "open(p,'a').write(json.dumps({'ts': time.time(), 'source':"
                     " 'multiagent-stack:router:tester', 'calls': 5}) + '\\n')\n")
    # the role's windowing logic, same expression as team/roles/tester.md line 23
    delta = tmp_path / "delta.py"
    delta.write_text("import json,os\n"
                     "P=os.environ['MULTIAGENT_LEDGER_PATH']\n"
                     "t=float(os.environ.get('TASK_START_TS') or 0)\n"
                     "M=[json.loads(l) for l in open(P) if l.strip()]\n"
                     "S=lambda rs: sum(int(r.get('calls',1)) for r in rs)\n"
                     "print('spent_since_start', S([r for r in M if float("
                     "r.get('ts',0)) >= t]))\n"
                     "print('session_cumulative', S(M))\n"
                     "print('window set' if os.environ.get('TASK_START_TS') "
                     "else 'TASK_START_TS unset')\n")
    calls = []
    install_oh_stubs(monkeypatch, calls)
    monkeypatch.setenv("MULTIAGENT_LEDGER_PATH", str(ledger))
    gate = f"python3 {spend} && python3 {delta}"
    res = tk.run_team(brief="B", workspace=str(repo), budget_calls=10,
                      roles="none", contract={"gate": gate},
                      waive_lessons="unit test")
    assert res["status"] == "GATE_PASS", res["gate_tail"]
    out = res["gate_tail"]
    assert "spent_since_start 5" in out, out
    assert "session_cumulative 19" in out, out
    assert "TASK_START_TS unset" not in out, out
    cfg = read_json(res, "config.json")
    assert cfg["task_start_ts"] <= int(now) + 1, cfg
