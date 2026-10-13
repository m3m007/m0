"""Offline tests for teamkit D4: the run-outcome split.

`outcome` answers a different question from `status`:

    status  = how the gate graded the work
    outcome = did the SYSTEM fail, or did the SYSTEM do nothing

The ~10-minute flash-lane stall class (run finishes, gate fails, zero commits,
zero diff) is an *abstention*: it needs a lane fix, not a better brief, and it
must not inflate gate-failure stats. All five documented classifications are
pinned here, each triggered by a stubbed conversation (no network, no real
Conversation -- see tests/test_runrecord.py for the stub mechanics) driving
REAL git inside a throwaway repo under tmp_path, through the knobs
TEAMKIT_RUNS_DIR / TEAMKIT_BASE_TMP.

Traps deliberately covered:
  * pre-existing dirt (tracked or untracked) must NOT read as partial work --
    digests are compared, presence is not;
  * a workspace with no usable git baseline is never called `abstained`:
    "we cannot see" is not "nothing happened";
  * dirt created by the GATE itself (a test-suite cache) is not team work;
  * `refused` covers every zero-spend early return, including an ERROR raised
    before the conversation started;
  * `outcome` is in summary.json on every path that writes a record, and the
    pre-existing keys keep their meaning.
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


def git(*args, cwd):
    return subprocess.run(['git'] + GIT_ID + list(args), cwd=str(cwd),
                          capture_output=True, text=True)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Autouse: records + baseline temp dirs stay inside tmp_path (R1 style)."""
    root = tmp_path / "runs"
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(root))
    monkeypatch.setenv("TEAMKIT_BASE_TMP", str(tmp_path / "basetmp"))
    monkeypatch.delenv("TEAMKIT_DRY_RUN", raising=False)
    (tmp_path / "basetmp").mkdir(exist_ok=True)
    monkeypatch.setattr(tk, "RUNS_DIR", str(root), raising=False)
    return root


@pytest.fixture
def repo(tmp_path):
    """A real, tiny, committed repo with one tracked file."""
    ws = tmp_path / "targetrepo"
    ws.mkdir()
    git("init", "-q", cwd=ws)
    (ws / "tracked.txt").write_text("baseline\n")
    git("add", "tracked.txt", cwd=ws)
    git("commit", "-qm", "baseline", cwd=ws)
    return ws


@pytest.fixture
def plain(tmp_path):
    """A directory that is NOT a git work tree."""
    ws = tmp_path / "plain-dir"
    ws.mkdir()
    return ws


def team_commits():
    """Stand-in for 'the team did real work and committed it'."""
    def _seam(workspace):
        Path(workspace, "team_edit.txt").write_text("work\n")
        git("add", "team_edit.txt", cwd=workspace)
        assert git("commit", "-qm", "team work", cwd=workspace).returncode == 0
    return _seam


def team_edits_tracked():
    """Work produced but NOT committed: a tracked diff during the run."""
    def _seam(workspace):
        Path(workspace, "tracked.txt").write_text("edited by the team\n")
    return _seam


def do_run(monkeypatch, ws, contract=None, calls=None, on_run=None, run_exc=None):
    """One whole run_team() with a stubbed conversation -> the returned dict."""
    calls = calls if calls is not None else []
    install_oh_stubs(monkeypatch, calls, run_exc=run_exc)
    return tk.run_team(brief="b", workspace=str(ws), budget_calls=10,
                       contract=contract, roles="none",
                       waive_lessons="unit test", on_run=on_run)


def summary(res):
    return json.loads((Path(res["run_dir"]) / "summary.json").read_text())


def only_summary(root):
    """The summary.json of the single run dir written so far."""
    dirs = [p for p in Path(root).iterdir() if p.is_dir() and p.name != ".locks"]
    assert len(dirs) == 1, dirs
    return json.loads((dirs[0] / "summary.json").read_text())


# ------------------------------------------------ the documented classification
def test_gate_pass_is_success(repo, monkeypatch):
    res = do_run(monkeypatch, repo, {"gate": "true"})
    assert res["status"] == "GATE_PASS"
    assert res["outcome"] == "success"


def test_gate_pass_new_is_success(repo, monkeypatch):
    """Pair check proves an improvement -> GATE_PASS_NEW -> success."""
    asks = "test -f team_edit.txt"
    res = do_run(monkeypatch, repo, {"gate": asks, "incumbent_gate": asks},
                 on_run=team_commits())
    assert res["status"] == "GATE_PASS_NEW"
    assert res["outcome"] == "success"
    assert res["work_evidence"]["commits"] == 1


def test_gate_fail_with_new_commits_is_partial_work(repo, monkeypatch):
    res = do_run(monkeypatch, repo, {"gate": "false"}, on_run=team_commits())
    assert res["status"] == "GATE_FAIL"
    assert res["outcome"] == "partial_work"
    assert res["work_evidence"]["commits"] >= 1


def test_gate_fail_with_uncommitted_diff_is_partial_work(repo, monkeypatch):
    """No commit, but a tracked diff produced during the run => there was work."""
    res = do_run(monkeypatch, repo, {"gate": "false"}, on_run=team_edits_tracked())
    assert res["status"] == "GATE_FAIL"
    assert res["outcome"] == "partial_work"
    ev = res["work_evidence"]
    assert ev["commits"] == 0 and ev["tracked_changed"] is True


def test_gate_fail_that_touched_nothing_is_abstained(repo, monkeypatch):
    """The flash-lane stall class: gate fails, zero commits, zero diff."""
    res = do_run(monkeypatch, repo, {"gate": "exit 7"})
    assert res["status"] == "GATE_FAIL"
    assert res["outcome"] == "abstained"
    assert res["work_evidence"] == {"commits": 0, "tracked_changed": False,
                                   "dirt_changed": False, "new_dirt": [],
                                   "measurable": True}


def test_run_done_no_gate_with_diff_is_partial_work(repo, monkeypatch):
    res = do_run(monkeypatch, repo, {}, on_run=team_edits_tracked())
    assert res["status"] == "RUN_DONE_NO_GATE"
    assert res["outcome"] == "partial_work"


def test_run_done_no_gate_that_touched_nothing_is_abstained(repo, monkeypatch):
    res = do_run(monkeypatch, repo, {})
    assert res["status"] == "RUN_DONE_NO_GATE"
    assert res["outcome"] == "abstained"


def test_error_that_touched_nothing_is_abstained(repo, monkeypatch, env):
    """A stuck conversation after real spend, with nothing to show: abstained."""
    with pytest.raises(RuntimeError, match="boom"):
        do_run(monkeypatch, repo, {"gate": "true"}, run_exc=RuntimeError("boom"))
    s = only_summary(env)
    assert s["status"] == "ERROR" and s["outcome"] == "abstained"
    assert s["work_evidence"]["commits"] == 0


def test_error_with_work_is_partial_work(repo, monkeypatch, env):
    def _edit(workspace):
        Path(workspace, "tracked.txt").write_text("half a job\n")
    with pytest.raises(RuntimeError, match="boom"):
        do_run(monkeypatch, repo, {"gate": "true"}, on_run=_edit,
               run_exc=RuntimeError("boom"))
    s = only_summary(env)
    assert s["status"] == "ERROR" and s["outcome"] == "partial_work"


def test_gate_pass_tie_is_tie_kept_not_success(repo, monkeypatch):
    """Retained but not proven better -- even when the team committed work."""
    res = do_run(monkeypatch, repo, {"gate": "true", "incumbent_gate": "true"},
                 on_run=team_commits())
    assert res["status"] == "GATE_PASS_TIE"
    assert res["outcome"] == "tie_kept"
    assert res["work_evidence"]["commits"] >= 1, "tie is not an emptiness verdict"


def test_budget_refused_is_refused(tmp_path, monkeypatch):
    calls: list = []
    install_oh_stubs(monkeypatch, calls)
    res = tk.run_team(brief="b", workspace=str(tmp_path),
                      budget_calls=tk.BUDGET_MAX_CALLS + 1, roles="none")
    assert res["status"] == "BUDGET_REFUSED" and res["outcome"] == "refused"
    assert calls == [], "zero spend must not pollute failure stats"


def test_dry_plan_is_refused(repo, monkeypatch, capsys):
    monkeypatch.setenv("TEAMKIT_DRY_RUN", "1")
    res = do_run(monkeypatch, repo, {"gate": "true"})
    assert res["status"] == "DRY_PLAN" and res["outcome"] == "refused"
    assert "DRY_PLAN" in capsys.readouterr().out


def test_error_before_any_spend_is_refused(repo, monkeypatch, env):
    """Preflight/lane blowups never reach the API: refused, not abstained."""
    calls: list = []
    install_oh_stubs(monkeypatch, calls, preflight_exc=ValueError("PREFLIGHT FAIL x"))
    with pytest.raises(ValueError, match="PREFLIGHT FAIL"):
        tk.run_team(brief="b", workspace=str(repo), budget_calls=10,
                    contract={"gate": "true"}, waive_lessons="unit test")
    assert calls == [("preflight",)], "no LLM, no conversation, no spend"
    s = only_summary(env)
    assert s["status"] == "ERROR" and s["outcome"] == "refused"


# ------------------------------------------------------ pre-existing dirt traps
def test_pre_existing_tracked_dirt_is_not_partial_work(repo, monkeypatch):
    """Dirt the owner already had must not be credited to the run."""
    (repo / "tracked.txt").write_text("the owner's own edit\n")
    res = do_run(monkeypatch, repo, {"gate": "false"})
    assert res["status"] == "GATE_FAIL"
    assert res["outcome"] == "abstained"
    assert res["work_evidence"]["tracked_changed"] is False
    assert (repo / "tracked.txt").read_text() == "the owner's own edit\n"


def test_pre_existing_untracked_file_is_not_partial_work(repo, monkeypatch):
    (repo / "scratch-untracked.txt").write_text("mine already\n")
    res = do_run(monkeypatch, repo, {"gate": "false"})
    assert res["outcome"] == "abstained"
    assert res["work_evidence"]["new_dirt"] == []


def test_pre_existing_untracked_plus_owner_edit_is_abstained(repo, monkeypatch):
    """Both kinds of pre-existing dirt together: still zero run-produced work."""
    (repo / "tracked.txt").write_text("owner\n")
    (repo / "junk.txt").write_text("owner untracked\n")
    res = do_run(monkeypatch, repo, {"gate": "false"})
    assert res["outcome"] == "abstained"


def test_new_untracked_file_during_the_run_is_partial_work(repo, monkeypatch):
    def _seam(workspace):
        Path(workspace, "produced.txt").write_text("team output\n")
    res = do_run(monkeypatch, repo, {"gate": "false"}, on_run=_seam)
    assert res["outcome"] == "partial_work"
    assert any("produced.txt" in l for l in res["work_evidence"]["new_dirt"])


def test_commit_then_reset_back_to_baseline_is_abstained(repo, monkeypatch):
    """Same HEAD at both ends: rev-list sees no work, and none was delivered."""
    def _seam(workspace):
        Path(workspace, "x.txt").write_text("x\n")
        git("add", "x.txt", cwd=workspace)
        git("commit", "-qm", "noise", cwd=workspace)
        git("reset", "--hard", "HEAD~1", cwd=workspace)
    res = do_run(monkeypatch, repo, {"gate": "false"}, on_run=_seam)
    assert res["outcome"] == "abstained"
    assert res["work_evidence"]["commits"] == 0


def test_gate_own_cache_dirt_is_not_team_work(repo, monkeypatch):
    """A gate that runs a test suite creates __pycache__ AFTER the team stopped;
    that must not relabel a stall as partial_work (evidence is a run-start vs
    team-finish comparison, never a post-gate one)."""
    gate = ("python3 -c \"import pathlib; pathlib.Path('__pycache__').mkdir();"
            " pathlib.Path('__pycache__/x.pyc').write_text('noise')\"; exit 1")
    res = do_run(monkeypatch, repo, {"gate": gate})
    assert res["status"] == "GATE_FAIL"
    assert (repo / "__pycache__").is_dir(), "the gate really did dirty the tree"
    assert res["outcome"] == "abstained", res["work_evidence"]
    assert res["work_evidence"]["dirt_changed"] is False


# ------------------------------------------------- unmeasurable is never abstention
def test_non_git_workspace_is_never_abstained(plain, monkeypatch):
    res = do_run(monkeypatch, plain, {"gate": "false"})
    assert res["status"] == "GATE_FAIL"
    assert res["outcome"] == "partial_work", "no baseline => cannot prove nothing"
    assert res["work_evidence"]["measurable"] is False


def test_unborn_repo_is_never_abstained(tmp_path, monkeypatch):
    ws = tmp_path / "fresh"
    ws.mkdir()
    git("init", "-q", cwd=ws)
    res = do_run(monkeypatch, ws, {"gate": "false"})
    assert res["status"] == "GATE_FAIL"
    assert res["outcome"] == "partial_work", "no HEAD at run start: unmeasurable"
    assert res["work_evidence"]["measurable"] is False


def test_work_outside_the_workspace_path_still_counts(repo, monkeypatch):
    """Digests are repo-wide on purpose: a sub-agent with a shell can edit the
    repo root, so that work must not be invisible to the outcome rule."""
    def _seam(workspace):
        Path(repo, "sibling.txt").write_text("edited elsewhere\n")
        git("add", "sibling.txt", cwd=repo)
        git("commit", "-qm", "sibling", cwd=repo)
    res = do_run(monkeypatch, repo, {"gate": "false"}, on_run=_seam)
    assert res["outcome"] == "partial_work"
    assert res["work_evidence"]["commits"] == 1


# ------------------------------------------------------------------- plumbing
def test_outcome_is_written_into_summary_json_on_every_path(repo, plain,
                                                            monkeypatch):
    paths = [
        do_run(monkeypatch, repo, {"gate": "true"}),                            # success
        do_run(monkeypatch, repo, {"gate": "true", "incumbent_gate": "true"}),  # tie
        do_run(monkeypatch, repo, {"gate": "false"}),                           # abstained
        do_run(monkeypatch, plain, {"gate": "false"}),                          # partial
    ]
    assert [p["outcome"] for p in paths] == ["success", "tie_kept",
                                             "abstained", "partial_work"]
    for res in paths:
        s = summary(res)
        assert s["outcome"] == res["outcome"]
        assert s["work_evidence"] == res["work_evidence"]
        # R4b-D6: the record carries `recall` on top of the returned dict.
        assert {k: v for k, v in s.items() if k != "recall"} == res, \
            "summary.json is the returned dict + run_dir + recall"


def test_outcome_survives_a_broken_recorder(repo, monkeypatch):
    """Callers read the dict; a dead recorder may not drop the key or change it."""
    good = do_run(monkeypatch, repo, {"gate": "false"})

    def boom(self, name, obj):
        raise RuntimeError("recorder is on fire")

    monkeypatch.setattr(tk.RunRecorder, "write_json", boom)
    broken = do_run(monkeypatch, repo, {"gate": "false"})
    assert broken["run_dir"] is None
    assert broken["outcome"] == good["outcome"] == "abstained"
    assert broken["status"] == good["status"]


def test_pre_existing_keys_keep_their_meaning(repo, monkeypatch):
    legacy = {"status", "gate", "gate_tail", "lessons", "workdir_diff",
              "recent_commits", "orchestrator_final"}
    res = do_run(monkeypatch, repo, {"gate": "true"})
    assert legacy <= set(res)
    assert res["gate"] == "true" and res["pair"] is None
    assert set(res) == legacy | {"run_dir", "pair", "outcome", "work_evidence"}


def test_outcome_never_mutates_the_workspace(repo, monkeypatch):
    """capture_workspace_state is READ-ONLY: no stash entry, no dangling object,
    workdir text identical before and after (no incumbent_gate involved)."""
    before = git("status", "--porcelain", cwd=repo).stdout
    do_run(monkeypatch, repo, {"gate": "true"})
    assert git("status", "--porcelain", cwd=repo).stdout == before
    assert git("stash", "list", cwd=repo).stdout.strip() == ""


def test_fingerprint_runs_without_incumbent_gate(repo, monkeypatch):
    """R1 captured porcelain only for v2 runs; D4 needs it on EVERY path."""
    seen = []
    real = tk.capture_workspace_state

    def spy(ws):
        seen.append(str(ws))
        return real(ws)

    monkeypatch.setattr(tk, "capture_workspace_state", spy)
    res = do_run(monkeypatch, repo, {"gate": "true"})      # no incumbent_gate
    assert seen, "run-start fingerprint must be taken for single-gate runs too"
    assert res["outcome"] == "success"


# -------------------------------------------------------- classify_outcome() unit
def test_classify_outcome_status_only_rows():
    for status, want in [("GATE_PASS", "success"), ("GATE_PASS_NEW", "success"),
                         ("GATE_PASS_TIE", "tie_kept"),
                         ("BUDGET_REFUSED", "refused"), ("DRY_PLAN", "refused")]:
        assert tk.classify_outcome(status, None) == want, status
    assert tk.classify_outcome("A_NEW_STATUS_SOMEDAY", None) == "partial_work"


def test_classify_outcome_uses_evidence_for_the_ambiguous_rows():
    clean = _fp(head="aaa", tracked="", porcelain="")
    committed = _fp(head="bbb", tracked="", porcelain="")
    edited = _fp(head="aaa", tracked="diff", porcelain="M tracked.txt")
    added = _fp(head="aaa", tracked="", porcelain="?? new.txt")
    for status in ("GATE_FAIL", "RUN_DONE_NO_GATE", "ERROR"):
        assert tk.classify_outcome(status, clean, dict(clean)) == "abstained"
        assert tk.classify_outcome(status, clean, committed) == "partial_work"
        assert tk.classify_outcome(status, clean, edited) == "partial_work"
        assert tk.classify_outcome(status, clean, added) == "partial_work"
        assert tk.classify_outcome(status, None, None) == "partial_work"


def _fp(head, tracked, porcelain, root="/x"):
    return {'root': root, 'ws': root, 'head': head, 'tracked': tracked,
            'tracked_sha': tk.sha256_text(tracked), 'porcelain': porcelain,
            'porcelain_sha': tk.sha256_text(porcelain)}


def test_work_evidence_counts_commits_ahead_of_the_baseline(repo):
    base = tk.capture_workspace_state(str(repo))
    for name in ("a.txt", "b.txt"):
        Path(repo, name).write_text(name + "\n")
        git("add", name, cwd=repo)
        git("commit", "-qm", name, cwd=repo)
    ev = tk.work_evidence(base)
    assert ev["commits"] == 2 and ev["measurable"] is True
    assert ev["tracked_changed"] is False and ev["new_dirt"] == []


def test_capture_workspace_state_shape_and_read_only(repo):
    (repo / "tracked.txt").write_text("dirty\n")
    (repo / "untracked.txt").write_text("new\n")
    before = git("status", "--porcelain", cwd=repo).stdout
    st = tk.capture_workspace_state(str(repo))
    assert set(st) == {"root", "ws", "head", "tracked", "tracked_sha",
                       "porcelain", "porcelain_sha"}
    assert st["root"] == str(repo)
    assert st["head"] == git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert "dirty" in st["tracked"]
    assert "untracked.txt" in st["porcelain"]
    assert st["tracked_sha"] == tk.sha256_text(st["tracked"])
    assert git("status", "--porcelain", cwd=repo).stdout == before
    assert git("stash", "list", cwd=repo).stdout.strip() == ""


def test_capture_workspace_state_outside_git(plain):
    st = tk.capture_workspace_state(str(plain))
    assert st["root"] is None and st["head"] == "" and st["porcelain"] == ""
    assert tk.work_evidence(st)["measurable"] is False


def test_outcome_values_are_the_documented_set():
    assert tk.OUTCOMES == ("success", "partial_work", "abstained", "refused",
                           "tie_kept")


def test_every_documented_outcome_is_reachable(repo, plain, tmp_path, monkeypatch):
    """One assertion per row of the README table, through run_team() itself."""
    seen = set()
    seen.add(do_run(monkeypatch, repo, {"gate": "true"})["outcome"])
    seen.add(do_run(monkeypatch, repo, {"gate": "true", "incumbent_gate": "true"},
                    on_run=team_commits())["outcome"])
    seen.add(do_run(monkeypatch, repo, {"gate": "false"},
                    on_run=team_edits_tracked())["outcome"])
    seen.add(do_run(monkeypatch, repo, {"gate": "false"})["outcome"])
    seen.add(do_run(monkeypatch, repo, {"gate": "true",
                                        "incumbent_gate": "true"})["outcome"])
    calls: list = []
    install_oh_stubs(monkeypatch, calls)
    seen.add(tk.run_team(workspace=str(tmp_path),
                         budget_calls=tk.BUDGET_MAX_CALLS + 1)["outcome"])
    assert seen == set(tk.OUTCOMES), seen


def test_non_git_workspace_with_passing_gate_is_success(plain, monkeypatch):
    """success is a status verdict, not a git verdict: no repo, still success."""
    res = do_run(monkeypatch, plain, {"gate": "true"})
    assert res["outcome"] == "success" and res["work_evidence"]["measurable"] is False


def test_dirty_tracked_files_with_incumbent_gate_still_abstain(repo, monkeypatch):
    """The nastiest combination: dirty tracked files + incumbent_gate, so run
    start also runs `git stash create`. The pair check grades against the stash
    commit, the outcome check grades against the fingerprint — and a team that
    touched nothing is still `abstained`, not `partial_work`."""
    (repo / "tracked.txt").write_text("owner edit\n")
    res = do_run(monkeypatch, repo,
                 {"gate": "grep -q nothing-here tracked.txt",
                  "incumbent_gate": "grep -q owner tracked.txt"})
    assert res["status"] == "GATE_FAIL", res["pair"]
    assert res["pair"]["baseline_ref"] != git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert res["outcome"] == "abstained", res["work_evidence"]
    assert (repo / "tracked.txt").read_text() == "owner edit\n"
    assert git("stash", "list", cwd=repo).stdout.strip() == ""


def test_dirty_tracked_files_then_team_edit_is_partial_work(repo, monkeypatch):
    """Same starting dirt, but the run adds its own change on top: the tracked
    digest moves, so this is partial_work."""
    (repo / "tracked.txt").write_text("owner edit\n")

    def _seam(workspace):
        Path(workspace, "note.txt").write_text("team note\n")
    res = do_run(monkeypatch, repo, {"gate": "false"}, on_run=_seam)
    assert res["status"] == "GATE_FAIL" and res["outcome"] == "partial_work"
    assert res["work_evidence"]["new_dirt"] == ["?? note.txt"]


def test_abstained_error_record_carries_evidence(repo, monkeypatch, env):
    with pytest.raises(RuntimeError, match="stuck"):
        do_run(monkeypatch, repo, {"gate": "true"},
               run_exc=RuntimeError("stuck loop"))
    s = only_summary(env)
    assert s["outcome"] == "abstained"
    assert s["work_evidence"]["measurable"] is True
    assert s["work_evidence"]["commits"] == 0


def test_subdir_workspace_outcome_follows_the_repo(repo, tmp_path, monkeypatch):
    """Workspace = a subdirectory of a real repo: the fingerprint resolves the
    work tree, so a stall there is still measurable as a stall."""
    sub = repo / "pkg" / "deep"
    sub.mkdir(parents=True)
    Path(repo, "pkg", "mod.py").write_text("x = 1\n")
    git("add", "pkg/mod.py", cwd=repo)
    git("commit", "-qm", "pkg", cwd=repo)
    res = do_run(monkeypatch, sub, {"gate": "exit 1"})
    assert res["status"] == "GATE_FAIL"
    assert res["outcome"] == "abstained", res["work_evidence"]


def test_refused_paths_write_no_record_but_carry_outcome(tmp_path, monkeypatch,
                                                        env):
    """BUDGET_REFUSED stays side-effect free (R1 contract) yet still labels
    itself `refused`, so a chokepoint can never be counted as a stall."""
    calls: list = []
    install_oh_stubs(monkeypatch, calls)
    res = tk.run_team(workspace=str(tmp_path / "nowhere"), budget_calls=999)
    assert res["outcome"] == "refused" and res["run_dir"] is None
    assert calls == []
    assert not env.exists() or list(env.iterdir()) == [], "no record, no lock"
