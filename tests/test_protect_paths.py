"""R5-D2: protected-path integrity — external success, not agent self-report.

A fixer that rewrites the test it was supposed to satisfy has fixed nothing, and
a gate run against its own edited tests will happily say GATE_PASS. `protect:`
inverts that: after the run and BEFORE the gate, everything the run changed
(committed diff since the run-start HEAD plus working dirt) must either fall
outside every protected path or be covered by a task's own declared paths.
One violation fails the run regardless of the gate's exit code.

Path matching is containment, in ONE helper shared with assert_disjoint:
`tests/` protects `tests/test_x.py`, `./tests/` protects it too, and `pkg` does
not protect `pkgfoo.py`. The same containment is what fixes the old string-
equality disjointness bug, where `tests/` and `tests/test_x.py` were "disjoint".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from team.run_team import (AgentResult, Task, Team, TeamError,  # noqa: E402
                           assert_disjoint, changed_paths,
                           capture_workspace_state, covers_path, norm_path,
                           protect_violations)

GIT_ID = ["-c", "user.email=protect@test", "-c", "user.name=protect@test"]

SPEC_TAMPER = json.dumps([
    {"id": "T1", "role": "fixer", "instruction": "Fix the bug.",
     "paths": ["pkg/a.py"], "acceptance": ["python3 -c 'pass'"]},
])
#: The same task, but allowed to edit the suite as well.
SPEC_DECLARED = json.dumps([
    {"id": "T1", "role": "fixer", "instruction": "Fix the bug and its test.",
     "paths": ["pkg/a.py", "tests/"], "acceptance": ["python3 -c 'pass'"]},
])


def git(*args, cwd):
    return subprocess.run(["git"] + GIT_ID + list(args), cwd=str(cwd),
                          capture_output=True, text=True)


@pytest.fixture(autouse=True)
def runs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(tmp_path / "runs"))
    return tmp_path / "runs"


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "target"
    (ws / "pkg").mkdir(parents=True)
    (ws / "tests").mkdir()
    git("init", "-q", cwd=ws)
    (ws / "pkg" / "a.py").write_text("BROKEN\n")
    (ws / "tests" / "test_a.py").write_text("assert True\n")
    (ws / "BRIEF.md").write_text("fix pkg/a.py\n")
    git("add", ".", cwd=ws)
    git("commit", "-qm", "baseline", cwd=ws)
    return ws


def protected_run(monkeypatch, ws, contract, tamper, spec=SPEC_TAMPER):
    """Run the workflow, let the fake team edit `tamper`, then grade."""
    spec_path = Path(ws).parent / "SPEC.json"
    team = Team(ws, contract=contract)

    def fake_run(task):
        if task.id == "spec":
            spec_path.write_text(spec, encoding="utf-8")
            return AgentResult(id=task.id, role=task.role, ok=True, requests=1,
                               text=spec)
        if task.id == "T1" and tamper:
            for rel in tamper:
                (Path(ws) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(ws) / rel).write_text("changed by the team\n")
        return AgentResult(id=task.id, role=task.role, ok=True, requests=1)

    monkeypatch.setattr(team, "run_task", fake_run)
    return team.run_workflow(Path(ws) / "BRIEF.md", spec_file=spec_path)


# --------------------------------------------------------------- containment
def test_covers_path_directory_protects_children(tmp_path):
    assert covers_path("tests/", "tests/test_x.py", root=tmp_path)
    assert covers_path("tests", "tests/test_x.py", root=tmp_path)
    assert covers_path("./tests/", "./tests/sub/test_y.py", root=tmp_path)
    assert covers_path("tests/", "tests/", root=tmp_path)
    assert not covers_path("pkg", "pkgfoo.py", root=tmp_path)
    assert not covers_path("tests/test_x.py", "tests/", root=tmp_path)


def test_norm_path_strips_dot_slash_and_trailing_slash(tmp_path):
    a = norm_path("./tests/", tmp_path)
    b = norm_path("tests", tmp_path)
    c = norm_path(str(tmp_path / "tests" / "test_x.py"), tmp_path)
    assert a == b
    assert covers_path(a, c)
    assert os.path.isabs(a)


def test_shared_helper_is_the_same_one_assert_disjoint_uses():
    """D2's one-helper rule: teamkit re-exports the engine, not a copy."""
    from team import teamkit as tk

    assert tk.covers_path is covers_path
    assert tk.norm_path is norm_path
    assert tk.protect_violations is protect_violations


# -------------------------------------------------- the string-equality bug
def test_disjointness_now_uses_containment():
    """`tests/` and `tests/test_x.py` overlap. Exact-string compare said no."""
    with pytest.raises(TeamError, match="nested inside each other"):
        assert_disjoint([Task("T1", "tester", "a", paths=("tests/",)),
                         Task("T2", "fixer", "b", paths=("tests/test_x.py",))])


def test_disjoint_sibling_paths_still_pass():
    assert_disjoint([Task("T1", "fixer", "a", paths=("pkg/a.py",)),
                     Task("T2", "tester", "b", paths=("tests/test_a.py",))])
    assert_disjoint([Task("T1", "fixer", "a", paths=("./pkg/",)),
                     Task("T2", "tester", "b", paths=("docs/",))])


def test_same_task_repeating_a_path_is_not_an_overlap():
    assert_disjoint([Task("T1", "fixer", "a", paths=("x.py", "./x.py", "x/.."))])


def test_map_agents_refuses_a_nested_claim_before_spending(tmp_path):
    team = Team(tmp_path, dry_run=True)
    before = team._requests
    with pytest.raises(TeamError, match="nested inside each other"):
        team.map_agents([Task("T1", "fixer", "a", paths=("tests/",)),
                         Task("T2", "fixer", "b", paths=("tests/test_x.py",))])
    assert team._requests == before, "the check must precede any charge"


# ------------------------------------------------------- violation behaviour
def test_tampered_protected_file_fails_even_when_the_gate_passes(repo,
                                                                monkeypatch):
    res = protected_run(monkeypatch, repo,
                        {"gate": "python3 -c 'pass'", "protect": ["tests/"]},
                        ["tests/test_a.py"])
    assert res["status"] == "GATE_FAIL"
    assert res["reason"].startswith("protected-path-tampered:")
    assert "test_a.py" in res["reason"]
    assert res["violations"], "the report says what was breached"
    assert res["outcome"] == "partial_work", "real edits, wrong files"


def test_protected_single_file_is_not_the_only_shape(repo, monkeypatch):
    res = protected_run(monkeypatch, repo,
                        {"gate": "python3 -c 'pass'",
                         "protect": ["./tests/test_a.py"]},
                        ["tests/test_a.py"])
    assert res["status"] == "GATE_FAIL"


def test_declared_paths_cover_the_change_so_no_violation(repo, monkeypatch):
    """The SAME tamper is legal when a task declared the protected directory."""
    res = protected_run(monkeypatch, repo,
                        {"gate": "python3 -c 'pass'", "protect": ["tests/"]},
                        ["tests/test_a.py"], spec=SPEC_DECLARED)
    assert res["status"] == "GATE_PASS", res["violations"]
    assert res["violations"] == []


def test_untouched_protected_path_keeps_the_gate_verdict(repo, monkeypatch):
    res = protected_run(monkeypatch, repo,
                        {"gate": "python3 -c 'pass'", "protect": ["tests/"]},
                        ["pkg/a.py"])
    assert res["status"] == "GATE_PASS"
    assert res["violations"] == []


def test_committed_tamper_is_caught_not_only_dirt(repo, monkeypatch):
    """git diff <baseline>..HEAD is the committed half of the evidence."""
    def commit_tamper(ws):
        (Path(ws) / "tests" / "test_a.py").write_text("weaker test\n")
        git("add", "tests/test_a.py", cwd=ws)
        assert git("commit", "-qm", "relax the test", cwd=ws).returncode == 0

    spec_path = Path(repo).parent / "SPEC2.json"
    team = Team(repo, contract={"gate": "python3 -c 'pass'",
                                "protect": ["tests/"]})

    def fake_run(task):
        if task.id == "spec":
            spec_path.write_text(SPEC_TAMPER, encoding="utf-8")
            return AgentResult(id=task.id, role=task.role, ok=True, requests=1,
                               text=SPEC_TAMPER)
        return AgentResult(id=task.id, role=task.role, ok=True, requests=1)

    monkeypatch.setattr(team, "run_task", fake_run)
    res = team.run_workflow(repo / "BRIEF.md", spec_file=spec_path,
                            on_run=commit_tamper)
    assert res["status"] == "GATE_FAIL"
    assert "test_a.py" in res["reason"]


def test_owner_pre_existing_dirt_is_not_blamed_on_the_run(repo, monkeypatch):
    """The owner's own edit is in the run-start fingerprint and must be ignored."""
    (repo / "tests" / "test_a.py").write_text("the owner's edit\n")
    res = protected_run(monkeypatch, repo,
                        {"gate": "python3 -c 'pass'", "protect": ["tests/"]},
                        ["pkg/a.py"])
    assert res["status"] == "GATE_PASS", res["violations"]


def test_no_protect_key_is_zero_regression(repo, monkeypatch):
    res = protected_run(monkeypatch, repo, {"gate": "python3 -c 'pass'"},
                        ["tests/test_a.py"])
    assert res["status"] == "GATE_PASS"
    assert res["violations"] == [] and res["protect"] is None


def test_violation_fails_a_run_with_no_gate_at_all(repo, monkeypatch):
    """Integrity outranks 'there was nothing to check': still GATE_FAIL."""
    res = protected_run(monkeypatch, repo, {"protect": ["tests/"]},
                        ["tests/test_a.py"])
    assert res["status"] == "GATE_FAIL"
    assert res["reason"].startswith("protected-path-tampered:")


def test_changed_paths_lists_committed_and_new_dirt(repo):
    base = capture_workspace_state(str(repo))
    (repo / "pkg" / "a.py").write_text("committed change\n")
    git("add", "pkg/a.py", cwd=repo)
    git("commit", "-qm", "work", cwd=repo)
    (repo / "scratch.txt").write_text("untracked\n")
    after = capture_workspace_state(str(repo))
    paths = changed_paths(base, after, repo)
    names = [Path(p).name for p in paths]
    assert "a.py" in names and "scratch.txt" in names, paths
    assert all(os.path.isabs(p) for p in paths)


def test_protect_violations_helper_is_pure(tmp_path):
    changed = [str(tmp_path / "tests" / "test_a.py"),
               str(tmp_path / "pkg" / "a.py")]
    bad = protect_violations(["tests/"], [], changed, tmp_path)
    assert len(bad) == 1 and "test_a.py" in bad[0]
    ok_tasks = [Task("T1", "tester", "x", paths=("tests/",))]
    assert protect_violations(["tests/"], ok_tasks, changed, tmp_path) == []
    assert protect_violations([], [], changed, tmp_path) == []
