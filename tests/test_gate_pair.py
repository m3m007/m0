"""Offline tests for teamkit D2: component check + pair acceptance vs incumbent.

Zero network and zero real Conversation (openhands is stubbed through
sys.modules -- see tests/test_runrecord.py for the stub mechanics). Everything
else is REAL git: tiny throwaway repos in tmp_path, real `git worktree add
--detach` baseline materialization, real gate subprocesses.

What is asserted, and why it matters:
  * the three v2 statuses are the exact strings, and legacy GATE_PASS is never
    emitted once incumbent_gate is present;
  * legacy callers (gate only) keep byte-identical behaviour apart from pair/run_dir;
  * baseline_ref is captured AT RUN START -- what the team does afterwards must
    not move the bar the work is graded against (proven through the `on_run`
    seam, which fires where conv.run() would have edited the workspace);
  * the baseline really is pristine: untracked files are absent from it, and the
    user's workdir is never mutated for the pair check (no stash apply, no
    checkout, no stash entry);
  * the materialized tree lives OUTSIDE runs/ and is cleaned up on every path,
    including a failing component check;
  * the `git archive | tar -x` fallback is exercised, because `git worktree add`
    is refused on some trees and that must not sink the run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from team import teamkit as tk  # noqa: E402
from tests.test_runrecord import install_oh_stubs  # noqa: E402

GIT_ID = ["-c", "user.email=teamkit-test@local", "-c", "user.name=teamkit-test"]

# The component check asks for the team's own artifact; the incumbent check asks
# for the same artifact in the pristine baseline, where it cannot exist. So
# "gate true/false" drives the pure status matrix, and team_edit.txt drives the
# "did we grade against the run-start tree" tests.
LEGACY_KEYS = {"status", "gate", "gate_tail", "lessons", "workdir_diff",
               "recent_commits", "orchestrator_final"}


def git(*args, cwd):
    return subprocess.run(['git'] + GIT_ID + list(args), cwd=str(cwd),
                          capture_output=True, text=True)


def worktree_paths(root):
    out = git("worktree", "list", cwd=root).stdout
    return [ln.split()[0] for ln in out.splitlines() if ln.strip()]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Autouse: records + baseline temp dirs + plan output all stay in tmp_path."""
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("TEAMKIT_BASE_TMP", str(tmp_path / "basetmp"))
    monkeypatch.delenv("TEAMKIT_DRY_RUN", raising=False)
    (tmp_path / "basetmp").mkdir(exist_ok=True)


@pytest.fixture
def repo(tmp_path):
    """A real, tiny, committed repo whose gates can probe tracked content."""
    ws = tmp_path / "targetrepo"
    ws.mkdir()
    git("init", "-q", cwd=ws)
    (ws / "tracked.txt").write_text("baseline\n")
    git("add", "tracked.txt", cwd=ws)
    git("commit", "-qm", "baseline", cwd=ws)
    return ws


def do_run(monkeypatch, ws, contract, calls=None, on_run=None):
    """One whole run_team() with a stubbed conversation -> the returned dict."""
    calls = calls if calls is not None else []
    install_oh_stubs(monkeypatch, calls)
    return tk.run_team(brief="b", workspace=str(ws), budget_calls=10,
                       contract=contract, roles="none",
                       waive_lessons="unit test", on_run=on_run)


def retained(res):
    return json.loads((Path(res["run_dir"]) / "retained.json").read_text())


def state(res):
    return json.loads((Path(res["run_dir"]) / "state.json").read_text())


def baseline_leftovers(tmp_path):
    return [p.name for p in (tmp_path / "basetmp").glob("teamkit-baseline-*")]


# ------------------------------------------------------------ status matrix
def test_legacy_single_gate_pass_status_is_unchanged(monkeypatch, repo):
    res = do_run(monkeypatch, repo, {"gate": "true"})
    assert res["status"] == "GATE_PASS", "legacy status string kept"
    assert res["pair"] is None, "no incumbent_gate => pair stays null"
    assert set(res) == LEGACY_KEYS | {"run_dir", "pair"}


def test_legacy_single_gate_fail_status_is_unchanged(monkeypatch, repo):
    res = do_run(monkeypatch, repo, {"gate": "false"})
    assert res["status"] == "GATE_FAIL" and res["pair"] is None


def test_pair_pass_pass_is_tie(monkeypatch, repo):
    res = do_run(monkeypatch, repo, {"gate": "true", "incumbent_gate": "true"})
    assert res["status"] == "GATE_PASS_TIE"
    assert res["pair"]["g_new"]["ok"] is True
    assert res["pair"]["g_base"]["ok"] is True
    head = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert res["pair"]["baseline_ref"] == head
    assert retained(res) == {"accepted": True, "baseline_ref": head,
                             "head_ref": head, "g_new": res["pair"]["g_new"],
                             "g_base": res["pair"]["g_base"]}


def test_pair_pass_fail_is_new(monkeypatch, repo):
    res = do_run(monkeypatch, repo, {"gate": "true", "incumbent_gate": "false"})
    assert res["status"] == "GATE_PASS_NEW"
    assert res["pair"]["g_new"]["ok"] is True and res["pair"]["g_base"]["ok"] is False
    r = retained(res)
    assert r["accepted"] is True, "improvement retained"
    assert r["g_new"]["ok"] is True and r["g_base"]["ok"] is False
    assert r["head_ref"] == r["baseline_ref"] == res["pair"]["baseline_ref"]


def test_pair_new_fails_is_gate_fail_with_incumbent_retained(monkeypatch, repo):
    res = do_run(monkeypatch, repo, {"gate": "false", "incumbent_gate": "true"})
    assert res["status"] == "GATE_FAIL"
    r = retained(res)
    assert r["accepted"] is False, "incumbent retained: the new work is not kept"
    assert r["g_new"]["ok"] is False and r["g_base"]["ok"] is True
    assert res["pair"]["g_base"]["ok"] is True, "incumbent still proven healthy"


def test_legacy_gate_pass_string_is_never_emitted_in_v2(monkeypatch, repo):
    statuses = {do_run(monkeypatch, repo, c)["status"] for c in
                ({"gate": "true", "incumbent_gate": "true"},
                 {"gate": "true", "incumbent_gate": "false"},
                 {"gate": "false", "incumbent_gate": "true"})}
    assert statuses == {"GATE_PASS_TIE", "GATE_PASS_NEW", "GATE_FAIL"}
    assert "GATE_PASS" not in statuses


def test_pair_sub_dict_shape_is_exact(monkeypatch, repo):
    res = do_run(monkeypatch, repo, {"gate": "true", "incumbent_gate": "false"})
    assert set(res["pair"]) == {"g_new", "g_base", "baseline_ref"}
    assert set(res["pair"]["g_new"]) == {"ok", "rc", "cmd", "error"}
    assert res["pair"]["g_new"]["rc"] == 0 and res["pair"]["g_base"]["rc"] == 1
    assert res["pair"]["g_new"]["cmd"] == "true"
    assert res["pair"]["g_base"]["cmd"] == "false"


def test_summary_json_records_the_pair_verdict(monkeypatch, repo):
    res = do_run(monkeypatch, repo, {"gate": "true", "incumbent_gate": "false"})
    summary = json.loads((Path(res["run_dir"]) / "summary.json").read_text())
    assert summary == res
    assert summary["pair"]["g_base"]["ok"] is False


# ------------------------------------------------------ baseline at run start
def test_baseline_ref_does_not_move_when_the_team_commits(monkeypatch, repo,
                                                          tmp_path):
    head_before = git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    def team_commits(ws):
        Path(ws, "team_edit.txt").write_text("work\n")
        git("add", "team_edit.txt", cwd=ws)
        assert git("commit", "-qm", "team work", cwd=ws).returncode == 0

    asks = "test -f team_edit.txt"
    res = do_run(monkeypatch, repo, {"gate": asks, "incumbent_gate": asks},
                 on_run=team_commits)
    assert res["pair"]["baseline_ref"] == head_before, \
        "baseline is the run-start ref, not the HEAD the team produced"
    assert git("rev-parse", "HEAD", cwd=repo).stdout.strip() != head_before, \
        "the team commit must actually have happened (fixture sanity)"
    # g_new sees the team's file, the pristine baseline does not => improvement
    assert res["pair"]["g_new"]["ok"] is True
    assert res["pair"]["g_base"]["ok"] is False
    assert res["status"] == "GATE_PASS_NEW", res["pair"]


def test_baseline_ref_is_a_stash_commit_when_tracked_files_are_dirty(repo):
    """Dirty tracked work: baseline_ref is a `stash create` COMMIT (not HEAD),
    and capture leaves the workdir exactly as found. Compared by object TYPE
    because stash commits are timestamped, so two calls can differ in sha."""
    (repo / "tracked.txt").write_text("local edit\n")
    assert git("status", "--porcelain", cwd=repo).stdout.strip(), "fixture dirty"
    ref = tk.capture_baseline_ref(str(repo))
    head = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert ref and ref != head, "a dirty tree must not be graded against bare HEAD"
    assert git("cat-file", "-t", ref, cwd=repo).stdout.strip() == "commit"
    # the stash commit's tree carries the tracked edit; HEAD's does not
    assert "local edit" in git("show", ref + ":tracked.txt", cwd=repo).stdout
    assert "baseline" in git("show", head + ":tracked.txt", cwd=repo).stdout
    # capture must NOT stash the work away
    assert (repo / "tracked.txt").read_text() == "local edit\n"
    assert git("stash", "list", cwd=repo).stdout.strip() == ""


def test_baseline_ref_is_head_when_workdir_is_clean(repo):
    assert tk.capture_baseline_ref(str(repo)) == \
        git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def test_untracked_only_dirt_is_not_part_of_the_baseline(monkeypatch, repo):
    """README rule: baseline means committed/stashed state ONLY. An incumbent
    gate that needs the team's UNTRACKED file therefore fails => NEW."""
    (repo / "team_edit.txt").write_text("work\n")      # untracked at run start
    asks = "test -f team_edit.txt"
    res = do_run(monkeypatch, repo, {"gate": asks, "incumbent_gate": asks})
    assert res["pair"]["g_new"]["ok"] is True
    assert res["pair"]["g_base"]["ok"] is False
    assert res["status"] == "GATE_PASS_NEW"
    assert (repo / "team_edit.txt").exists(), "workspace untouched by the check"


def test_g_base_runs_the_incumbent_gate_not_the_component_gate(monkeypatch, repo):
    """Both cmds run where they belong: gate in the workspace, incumbent_gate in
    the pristine tree. Reversing them would silently grade nothing."""
    res = do_run(monkeypatch, repo,
                 {"gate": "test -f tracked.txt",          # true in both trees
                  "incumbent_gate": "grep -q baseline tracked.txt"})
    assert res["pair"]["g_base"]["ok"] is True, \
        "pristine baseline still has 'baseline' in tracked.txt"
    assert res["status"] == "GATE_PASS_TIE"


# ------------------------------------------- materialization, purity, cleanup
def test_materialized_baseline_is_pristine_and_outside_runs(repo, tmp_path):
    ref = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "untracked-only.txt").write_text("not in baseline\n")
    (repo / "tracked.txt").write_text("local edit\n")
    bdir, cleanup = tk.materialize_baseline(ref, str(repo))
    try:
        assert Path(bdir).is_dir()
        assert not str(Path(bdir).resolve()).startswith(str(tmp_path / "runs")), \
            "baseline tree must live OUTSIDE runs/"
        assert Path(bdir).resolve() != repo.resolve()
        assert (Path(bdir) / "tracked.txt").read_text() == "baseline\n", \
            "committed content, not the local edit"
        assert not (Path(bdir) / "untracked-only.txt").exists(), \
            "untracked files are NOT part of the baseline (by design; README)"
    finally:
        cleanup()
    assert not Path(bdir).exists(), "temp baseline tree removed"
    assert worktree_paths(repo) == [str(repo)], "no leftover git worktrees"
    assert baseline_leftovers(tmp_path) == []


def test_fallback_to_git_archive_when_worktree_add_is_refused(monkeypatch, repo,
                                                               tmp_path):
    """`git worktree add` is refused on some trees (dirty, untracked-only);
    `git archive | tar -x` must carry the pair check instead of failing it."""
    ref = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    real = subprocess.run
    seen = []

    def fake(args, *a, **kw):
        if isinstance(args, list) and "worktree" in args and "add" in args:
            seen.append(list(args))
            return real(["/usr/bin/false"])          # simulate the refusal
        return real(args, *a, **kw)

    monkeypatch.setattr(tk.subprocess, "run", fake)
    bdir, cleanup = tk.materialize_baseline(ref, str(repo))
    try:
        assert seen, "the test must really have refused worktree add"
        assert (Path(bdir) / "tracked.txt").read_text() == "baseline\n"
        assert worktree_paths(repo) == [str(repo)], "fallback leaves no worktree"
    finally:
        cleanup()
    assert not Path(bdir).exists()
    assert baseline_leftovers(tmp_path) == []


def test_pair_check_never_mutates_the_user_workspace(monkeypatch, repo, tmp_path):
    (repo / "tracked.txt").write_text("local edit\n")
    before_status = git("status", "--porcelain", cwd=repo).stdout
    before_stash = git("stash", "list", cwd=repo).stdout
    res = do_run(monkeypatch, repo,
                 {"gate": "grep -q 'local edit' tracked.txt",
                  "incumbent_gate": "grep -q baseline tracked.txt"})
    assert res["status"] == "GATE_PASS_NEW", res["pair"]
    assert (repo / "tracked.txt").read_text() == "local edit\n"
    assert git("status", "--porcelain", cwd=repo).stdout == before_status
    assert git("stash", "list", cwd=repo).stdout == before_stash == ""
    assert worktree_paths(repo) == [str(repo)]
    assert baseline_leftovers(tmp_path) == []


def test_worktree_cleaned_up_even_when_component_check_fails(monkeypatch, repo,
                                                              tmp_path):
    res = do_run(monkeypatch, repo, {"gate": "false",
                                     "incumbent_gate": "grep -q baseline tracked.txt"})
    assert res["status"] == "GATE_FAIL"
    assert worktree_paths(repo) == [str(repo)]
    assert baseline_leftovers(tmp_path) == []
    st = state(res)
    assert st["gate"]["status"] == "fail" and st["gate"]["attempts"] == 1


def test_run_dir_contains_retained_json_for_v2_only(monkeypatch, repo):
    legacy = do_run(monkeypatch, repo, {"gate": "true"})
    assert not (Path(legacy["run_dir"]) / "retained.json").exists()
    v2 = do_run(monkeypatch, repo, {"gate": "true", "incumbent_gate": "false"})
    assert set(retained(v2)) == {"accepted", "baseline_ref", "head_ref",
                                 "g_new", "g_base"}


def test_unrunnable_pair_check_claims_no_improvement(monkeypatch, tmp_path):
    """Not a git work tree => no baseline => the incumbent is unknown, so
    GATE_PASS_NEW must NOT be shouted; the reason is recorded, not hidden."""
    ws = tmp_path / "plain-dir"
    ws.mkdir()
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(tmp_path / "runs"))
    res = do_run(monkeypatch, ws, {"gate": "true", "incumbent_gate": "false"})
    assert res["status"] == "GATE_PASS_TIE"
    assert res["pair"]["baseline_ref"] is None
    assert res["pair"]["g_base"]["ran"] is False
    assert "not a git work tree" in res["pair"]["g_base"]["error"]
    assert retained(res)["accepted"] is True


def test_gate_stage_skipped_when_contract_has_no_gate(monkeypatch, repo):
    res = do_run(monkeypatch, repo, {})
    assert res["status"] == "RUN_DONE_NO_GATE" and res["pair"] is None
    assert state(res)["gate"]["status"] == "skipped"
