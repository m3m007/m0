"""R5-D1: the post-run grading stage inside Team.run_workflow, offline.

One engine now grades every run. These tests drive the REAL workflow with
`run_task` stubbed (zero requests, zero Conversation), against real throwaway git
repos, and assert the five statuses, the outcome split from git evidence, the
legacy single-gate path, the run record, and the CLI exit map.

    spec -> fixers -> reviewer -> [grading stage: gate, pair, outcome, record]

What is deliberately NOT stubbed: git, the gate subprocess, and the record files.
Those ARE the discipline being ported, so a test that faked them would prove
nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from team.run_team import (AgentResult, Task, Team, TeamError,  # noqa: E402
                           classify_outcome, grade_exit_code, work_evidence)
from team import teamkit as tk  # noqa: E402
from tests.test_runrecord import install_oh_stubs  # noqa: E402

GIT_ID = ["-c", "user.email=gate@test", "-c", "user.name=gate@test"]

#: A two-task spec the fake architect "writes", with the third task depending on
#: both, so depends_on ordering is exercised on the same path as the grading.
SPEC = json.dumps([
    {"id": "T1", "role": "fixer", "instruction": "Fix durations.",
     "paths": ["pkg/a.py"], "acceptance": ["python3 -c 'pass'"]},
    {"id": "T2", "role": "fixer", "instruction": "Fix normalise.",
     "paths": ["pkg/b.py"], "acceptance": ["python3 -c 'pass'"]},
])


def git(*args, cwd):
    return subprocess.run(["git"] + GIT_ID + list(args), cwd=str(cwd),
                          capture_output=True, text=True)


@pytest.fixture(autouse=True)
def runs_env(tmp_path, monkeypatch):
    """No test here may write into the repo's own runs/ evidence tree."""
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("TEAMKIT_DRY_RUN", raising=False)
    return tmp_path / "runs"


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "target"
    ws.mkdir()
    git("init", "-q", cwd=ws)
    (ws / "tracked.txt").write_text("baseline\n")
    git("add", "tracked.txt", cwd=ws)
    git("commit", "-qm", "baseline", cwd=ws)
    (ws / "BRIEF.md").write_text("two fixes, one file each\n")
    return ws


def graded_run(monkeypatch, ws, contract, *, edit=None, brief=None, spec_out=True,
               **kw):
    """One whole run_workflow through the grading stage, zero requests spent.

    `edit` stands in for the team's work: it runs at the point the workflow is
    finished and the work evidence is fingerprinted (the `on_run` seam), i.e.
    before the gate, which is the order the evidence split depends on.

    By default the fake architect's TEAM-SPEC.json lands OUTSIDE the workspace
    (`spec_out`), so a no-op run measures as the zero-work stall it is. The
    engine writes the spec into the workspace in a real run, and that file is
    honestly new dirt; tests that care about evidence say where the spec goes.
    """
    team = Team(ws, contract=contract, **kw)
    spec_path = Path(ws).parent / "TEAM-SPEC-external.json"
    seen = []

    def fake_run(task):
        seen.append(task.id)
        if task.id == "spec":
            spec_path.write_text(SPEC, encoding="utf-8")
            return AgentResult(id=task.id, role=task.role, ok=True, requests=1,
                               text=SPEC)
        return AgentResult(id=task.id, role=task.role, ok=True, requests=1,
                           actions=["BashAction"])

    monkeypatch.setattr(team, "run_task", fake_run)
    report = team.run_workflow(brief or (Path(ws) / "BRIEF.md"),
                               spec_file=(spec_path if spec_out else None),
                               on_run=(lambda p: edit(p) if edit else None))
    report["_seen"] = seen
    return report


def read(res, name):
    return json.loads((Path(res["run_dir"]) / name).read_text())


def team_commits(workspace):
    Path(workspace, "team_edit.txt").write_text("work\n")
    git("add", "team_edit.txt", cwd=workspace)
    git("commit", "-qm", "team work", cwd=workspace)


# ------------------------------------------------------------ graded report keys
def test_graded_report_keeps_every_existing_key_and_adds_the_new_ones(repo,
                                                                     monkeypatch):
    res = graded_run(monkeypatch, repo, {"gate": "true"})
    for key in ("steps", "requests_charged", "spec_file", "spec_source",
                "tasks_parsed", "spec_validation", "spec_attempts",
                "status_files", "cleared_stale_status_files"):
        assert key in res, f"existing report key {key} vanished"
    for key in ("status", "gate", "gate_tail", "pair", "outcome",
                "work_evidence", "run_dir"):
        assert key in res, f"D1 report key {key} missing"
    assert res["graded"] is True
    assert res["spec_source"] == "file" and res["tasks_parsed"] == 2


def test_no_contract_means_no_grading_and_no_new_keys(repo, monkeypatch):
    """Zero regression for the pre-R5 engine: no contract, no graded report."""
    res = graded_run(monkeypatch, repo, None)
    assert res.get("graded") is False
    assert "status" not in res and "outcome" not in res
    assert [s["id"] for s in res["steps"]] == ["spec", "T1", "T2", "review"]


# ---------------------------------------------------------------- status matrix
def test_legacy_single_gate_pass(repo, monkeypatch):
    res = graded_run(monkeypatch, repo, {"gate": "true"})
    assert res["status"] == "GATE_PASS"
    assert res["pair"] is None, "no incumbent_gate => no pair check"
    assert res["outcome"] == "success"


def test_single_gate_fail(repo, monkeypatch):
    res = graded_run(monkeypatch, repo, {"gate": "exit 3"})
    assert res["status"] == "GATE_FAIL"
    assert res["outcome"] == "abstained", "nothing touched: a stall, not a failure"
    assert res["work_evidence"]["commits"] == 0


def test_pair_pass_fail_is_new(repo, monkeypatch):
    asks = "test -f team_edit.txt"
    res = graded_run(monkeypatch, repo, {"gate": asks, "incumbent_gate": asks},
                     edit=team_commits)
    assert res["status"] == "GATE_PASS_NEW"
    assert res["pair"]["g_new"]["ok"] is True
    assert res["pair"]["g_base"]["ok"] is False
    assert res["outcome"] == "success"


def test_pair_pass_pass_is_tie_and_never_legacy_pass(repo, monkeypatch):
    res = graded_run(monkeypatch, repo, {"gate": "true", "incumbent_gate": "true"})
    assert res["status"] == "GATE_PASS_TIE"
    assert res["outcome"] == "tie_kept"
    statuses = {graded_run(monkeypatch, repo, c)["status"] for c in
                ({"gate": "true", "incumbent_gate": "true"},
                 {"gate": "true", "incumbent_gate": "false"},
                 {"gate": "false", "incumbent_gate": "true"})}
    assert statuses == {"GATE_PASS_TIE", "GATE_PASS_NEW", "GATE_FAIL"}
    assert "GATE_PASS" not in statuses


def test_pair_new_fails_keeps_the_incumbent(repo, monkeypatch):
    res = graded_run(monkeypatch, repo,
                     {"gate": "false", "incumbent_gate": "true"})
    assert res["status"] == "GATE_FAIL"
    assert res["retained"]["accepted"] is False


def test_gate_without_baseline_claims_no_improvement(repo, monkeypatch):
    """A pair check that cannot run proves nothing about the incumbent."""
    plain = repo.parent / "not-a-repo"
    plain.mkdir()
    (plain / "BRIEF.md").write_text("x\n")
    res = graded_run(monkeypatch, plain, {"gate": "true", "incumbent_gate": "false"})
    assert res["status"] == "GATE_PASS_TIE"
    assert res["pair"]["g_base"]["ran"] is False


def test_no_gate_command_is_run_done_no_gate(repo, monkeypatch):
    res = graded_run(monkeypatch, repo, {})
    assert res["status"] == "RUN_DONE_NO_GATE"
    assert res["outcome"] == "abstained"


# ------------------------------------------------------- baseline at run start
def test_baseline_does_not_move_when_the_team_commits(repo, monkeypatch):
    head_before = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    asks = "test -f team_edit.txt"
    res = graded_run(monkeypatch, repo, {"gate": asks, "incumbent_gate": asks},
                     edit=team_commits)
    assert res["pair"]["baseline_ref"] == head_before
    assert git("rev-parse", "HEAD", cwd=repo).stdout.strip() != head_before
    assert res["status"] == "GATE_PASS_NEW"


def test_evidence_is_fingerprinted_before_the_gate(repo, monkeypatch):
    """A gate that runs a test suite makes __pycache__ dirt; that is not work.

    The seam edits a tracked file, and the gate then writes an untracked file.
    The evidence must show the edit and must NOT show the gate's own dirt.
    """
    def _edit(ws):
        Path(ws, "tracked.txt").write_text("team edit\n")

    res = graded_run(monkeypatch, repo, {"gate": "touch gate_artifact.txt"},
                     edit=_edit)
    ev = res["work_evidence"]
    assert ev["tracked_changed"] is True
    assert not any("gate_artifact" in line for line in ev["new_dirt"]), ev


# ---------------------------------------------------------------- run records
def test_graded_run_writes_a_record_with_summary_and_state(repo, monkeypatch):
    res = graded_run(monkeypatch, repo, {"gate": "true"})
    run_dir = Path(res["run_dir"])
    assert run_dir.is_dir() and run_dir.parent == Path(
        __import__("os").environ["TEAMKIT_RUNS_DIR"])
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["status"] == "GATE_PASS"
    assert summary["run_dir"] == str(run_dir)
    cfg = json.loads((run_dir / "config.json").read_text())
    assert cfg["engine"] == "team"
    assert cfg["contract"] == {"gate": "true"}
    state = json.loads((run_dir / "state.json").read_text())
    assert set(state) == {"spec", "team_run", "gate", "lessons"}


def test_v2_run_writes_retained_json(repo, monkeypatch):
    res = graded_run(monkeypatch, repo, {"gate": "true", "incumbent_gate": "false"})
    retained = json.loads((Path(res["run_dir"]) / "retained.json").read_text())
    assert set(retained) == {"accepted", "baseline_ref", "head_ref",
                             "g_new", "g_base"}
    assert retained["accepted"] is True


def test_legacy_run_writes_no_retained_json(repo, monkeypatch):
    res = graded_run(monkeypatch, repo, {"gate": "true"})
    assert not (Path(res["run_dir"]) / "retained.json").exists()


def test_both_entry_points_write_into_one_runs_tree(repo, monkeypatch):
    """Acceptance #2: the CLI engine and the teamkit facade share runs/."""
    res_engine = graded_run(monkeypatch, repo, {"gate": "true"})
    calls = []
    install_oh_stubs(monkeypatch, calls)
    res_facade = tk.run_team(brief="b", workspace=str(repo), budget_calls=10,
                             contract={"gate": "true"}, roles="none",
                             waive_lessons="unit test")
    root = Path(__import__("os").environ["TEAMKIT_RUNS_DIR"])
    dirs = sorted(p.name for p in root.iterdir() if p.is_dir() and p.name != ".locks")
    assert len(dirs) == 2, dirs
    assert Path(res_engine["run_dir"]).parent == root
    assert Path(res_facade["run_dir"]).parent == root
    engine_cfg = json.loads((Path(res_engine["run_dir"]) / "config.json").read_text())
    facade_cfg = json.loads((Path(res_facade["run_dir"]) / "config.json").read_text())
    assert engine_cfg["engine"] == "team"
    assert facade_cfg["engine"]["name"] == "run_team.Team", "facade names the engine"


# ---------------------------------------------------------------- CLI exit map
@pytest.mark.parametrize("status,want", [
    ("GATE_PASS", 0), ("GATE_PASS_NEW", 0), ("GATE_PASS_TIE", 2),
    ("GATE_FAIL", 1), ("RUN_DONE_NO_GATE", 0), ("anything_else", 1)])
def test_exit_map_per_status(status, want):
    assert grade_exit_code(status) == want


def test_exit_map_abort_and_failed_steps():
    assert grade_exit_code("GATE_PASS", aborted=True) == 3
    assert grade_exit_code("GATE_PASS", failed_steps=1) == 1


def test_cli_exit_code_comes_from_the_grade(tmp_path, monkeypatch, capsys):
    """The CLI exits on the verdict, not on the step list."""
    from team.run_team import main

    monkeypatch.setattr(Team, "run_workflow",
                        lambda self, brief, **kw: {
                            "steps": [{"id": "spec", "role": "architect", "ok": True,
                                       "requests": 1}],
                            "requests_charged": 1, "graded": True,
                            "status": "GATE_PASS_TIE", "outcome": "tie_kept",
                            "gate_tail": "", "pair": None,
                            "work_evidence": None, "run_dir": None})
    brief = tmp_path / "BRIEF.md"
    brief.write_text("x\n")
    code = main(["--brief", str(brief), "--workspace", str(tmp_path),
                 "--gate", "true", "--no-record"])
    assert code == 2, capsys.readouterr().out
    assert "GATE_PASS_TIE" in capsys.readouterr().out


def test_cli_refusals_keep_their_historical_exit_code(tmp_path, capsys):
    """Backward compat: refusing before spend was, and stays, exit 2."""
    from team.run_team import main

    assert main(["--brief", str(tmp_path / "nope.md")]) == 2
    assert main(["--plan", "--brief", str(tmp_path / "nope.md"),
                 "--max-requests", "99999"]) == 2


# ---------------------------------------------------------- engine helpers
def test_classify_outcome_status_only_and_evidence_rows():
    assert classify_outcome("GATE_PASS", None) == "success"
    assert classify_outcome("GATE_PASS_TIE", None) == "tie_kept"
    assert classify_outcome("BUDGET_REFUSED", None) == "refused"
    assert classify_outcome("GATE_FAIL", None) == "partial_work"
    clean = {"commits": 0, "tracked_changed": False, "dirt_changed": False,
             "new_dirt": [], "measurable": True}
    assert classify_outcome("GATE_FAIL", clean) == "abstained"
    assert classify_outcome("GATE_FAIL",
                            {"commits": 1, "measurable": True}) == "partial_work"


def test_work_evidence_unmeasurable_is_never_a_stall(repo):
    base = work_evidence({"head": "", "root": str(repo)},
                         {"head": "", "root": str(repo)})
    assert base["measurable"] is False
    assert classify_outcome("GATE_FAIL", base) == "partial_work", \
        "cannot prove zero work: never claim abstention"


def test_dry_run_is_never_graded_and_never_spends(tmp_path):
    team = Team(tmp_path, dry_run=True, contract={"gate": "true"})
    report = team.run_workflow(tmp_path / "missing-brief.md")
    assert report["requests_charged"] == 0
    assert "status" not in report, "a dry run grades nothing"
