"""End-to-end smoke: architect -> 2 parallel fixers -> reviewer -> gates green.

Two modes, and the default one costs nothing:

    # offline, 0 requests:
    /usr/bin/python3 -m pytest tests/test_e2e_smoke.py -q
    # the real run, ~15-30 requests (this is the only working live command):
    SMOKE_LIVE=1 /usr/bin/python3 -m pytest tests/test_e2e_smoke.py -q -s

The interpreter is part of the command, not a detail. Observed live: the same
run started with the oh-venv python died instantly with "No module named
pytest", because the working direction is system python (which has pytest and
is what the gate uses) plus the SDK venv's site-packages, which the repo-root
conftest.py appends for bare /usr/bin/python3. The guard below refuses any
other interpreter under SMOKE_LIVE and prints this command line, rather than
letting the run die with an error that reads like a broken harness. Set
MULTIAGENT_PY to opt out of the check.

Why two modes. A smoke test that always spends quota gets run once and then
dreaded; one that costs nothing gets run every commit. The live path is the
opt-in, because on this plan every request is a line item against a hard window.

The scratch repo is tests/smoke_repo: two bugs in two SEPARATE modules so the
two fixers genuinely can run in parallel, plus a third task that depends on both
so the dependency ordering is exercised. It is a small synthetic repo rather than
the 5,500-line nf-audit on purpose — a giant target is not repeatable and spends
quota re-discovering wiring rather than testing it. Point SMOKE_REPO at a copy of
nf-audit (or pass --repo) when you want the real thing.

What is asserted:
  * every role produced real actions (not a "success" with nothing done)
  * the two fixers ran in parallel and touched disjoint files
  * the dependent task ran only after its dependencies
  * the target repo's own test suite goes green
  * the request count is recorded to tokenwatch and stays under the ceiling
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.probe_budget import ceiling_from_router, total  # noqa: E402
from team.run_team import Task, Team  # noqa: E402

def smoke_live() -> bool:
    """Is the live tier asked for? Read per call, so the guard is testable."""
    return os.environ.get("SMOKE_LIVE", "").strip().lower() in {"1", "true", "yes"}


LIVE = smoke_live()
SMOKE_SRC = REPO / "tests" / "smoke_repo"

#: The one interpreter the live suite works on. See the module docstring.
SYSTEM_PYTHON = "/usr/bin/python3"


def assert_live_interpreter(executable: str | None = None) -> None:
    """Refuse to run the live suite on an interpreter that cannot load pytest.

    Checked at collection time, before a single request can be spent, because
    the venv python fails the run with "No module named pytest" and the obvious
    reading of that is "the harness is broken" rather than "wrong interpreter".

    ``MULTIAGENT_PY`` overrides the comparison (point it at the interpreter you
    deliberately want), and nothing here touches the network or the filesystem.
    """
    if not smoke_live():
        return
    if os.environ.get("MULTIAGENT_PY", "").strip():
        return
    current = Path(executable or sys.executable or "").resolve()
    if current == Path(SYSTEM_PYTHON).resolve():
        return
    pytest.fail(
        f"SMOKE_LIVE needs the system python, not {current}. Run: "
        f"SMOKE_LIVE=1 {SYSTEM_PYTHON} -m pytest tests/test_e2e_smoke.py -q -s",
        pytrace=False)


assert_live_interpreter()

# The known-good task split for smoke_repo. Used in the offline run and as the
# spec the live run compares against, so "the architect produced a spec" is a
# checkable claim rather than a vibe.
EXPECTED_TASKS = [
    Task(id="A", role="fixer",
         instruction=("Fix humanise() in pkg/durations.py so zero components are "
                      "dropped, except that 0 renders as '0s'. Touch only "
                      "pkg/durations.py."),
         paths=("pkg/durations.py",),
         acceptance=("python3 -m pytest -q tests/test_durations.py",)),
    Task(id="B", role="fixer",
         instruction=("Fix slugify() in pkg/normalise.py so runs of "
                      "non-alphanumerics collapse to one hyphen and leading or "
                      "trailing hyphens are stripped. Touch only pkg/normalise.py."),
         paths=("pkg/normalise.py",),
         acceptance=("python3 -m pytest -q tests/test_normalise.py",)),
    Task(id="C", role="tester",
         instruction=("Run the whole suite in this repo and confirm both fixes "
                      "hold together. Touch only tests/."),
         paths=("tests/",), depends_on=("A", "B"),
         acceptance=("python3 -m pytest -q",)),
]


def fresh_workspace(tmp_path: Path) -> Path:
    """Copy the smoke repo so every run starts from the same known-broken state."""
    ws = tmp_path / "smoke"
    if ws.exists():
        shutil.rmtree(ws)
    shutil.copytree(SMOKE_SRC, ws, ignore=shutil.ignore_patterns("__pycache__",
                                                                "*.pyc"))
    return ws


def run_gates(workspace: Path) -> dict[str, str]:
    """Run the repo's own test suite. Returns {label: 'ok'|error}."""
    results = {}
    for label, cmd in (("durations", ["python3", "-m", "pytest", "-q",
                                      "tests/test_durations.py"]),
                       ("normalise", ["python3", "-m", "pytest", "-q",
                                     "tests/test_normalise.py"]),
                       ("whole", ["python3", "-m", "pytest", "-q"]),
                       ("compile", ["python3", "-m", "compileall", "-q", "pkg"])):
        proc = subprocess.run(cmd, cwd=str(workspace), capture_output=True,
                              text=True, check=False, timeout=180)
        results[label] = "ok" if proc.returncode == 0 else (
            f"exit {proc.returncode}: " + (proc.stdout or proc.stderr)[-400:])
    return results


# --------------------------------------------------------------- offline
def test_offline_dry_run_costs_nothing_and_wires_up(tmp_path, capsys):
    """The default run: full workflow walk, zero requests, no quota needed."""
    ws = fresh_workspace(tmp_path)
    before = total()
    team = Team(ws, dry_run=True)
    report = team.run_workflow(ws / "BRIEF.md")
    assert report["requests_charged"] == 0, "a dry run must never spend"
    assert total() == before, "a dry run must not touch the quota ledger"
    ids = [s["id"] for s in report["steps"]]
    assert ids == ["spec", "T1", "T2", "review"]
    assert all(s["ok"] for s in report["steps"])


def test_offline_parallel_tasks_are_disjoint():
    """The brief's shape: two fixers, independent files, safe to run together."""
    from team.run_team import assert_disjoint

    assert_disjoint(EXPECTED_TASKS)


def test_offline_dependency_wave_is_two_rounds():
    from team.run_team import order_waves

    waves = order_waves(EXPECTED_TASKS)
    assert [[t.id for t in w] for w in waves] == [["A", "B"], ["C"]], \
        "A and B must share a wave (parallel); C must wait for both"


# ---------------------------------------------------------------- live
@pytest.mark.skipif(not LIVE, reason="set SMOKE_LIVE=1 to spend real requests")
def test_live_team_workflow_fixes_the_repo(tmp_path):
    ws = fresh_workspace(tmp_path)
    ceiling = ceiling_from_router()
    before_ledger = total()

    # Prove the target is actually broken first, otherwise "tests pass" is
    # meaningless: a green suite after a no-op run would look like success.
    pre = run_gates(ws)
    assert "ok" not in (pre["durations"], pre["normalise"]), \
        f"smoke repo should start broken, got {pre}"

    team = Team(ws, max_requests=min(ceiling, 80))
    report = team.run_workflow(ws / "BRIEF.md")
    spent = total() - before_ledger

    print("\n--- SMOKE REPORT ---")
    print(json.dumps({k: v for k, v in report.items() if k != "steps"},
                     indent=2, default=str))
    for step in report["steps"]:
        print(f"  {step['id']:8s} {step['role']:10s} ok={step['ok']} "
              f"req={step['requests']} actions={step['actions']} "
              f"err={step['error']}")
    print(f"  ledger delta: {spent} request(s); ceiling {ceiling}")
    print("  gates:", json.dumps(run_gates(ws)))

    assert not report.get("aborted"), report.get("aborted")
    assert report.get("tasks_parsed", 0) >= 2, \
        f"architect must emit at least the two fix tasks: {report}"
    fixer_steps = [s for s in report["steps"] if s["role"] == "fixer"]
    assert fixer_steps, "no fixer step ran"
    assert all(s["ok"] for s in report["steps"]), \
        f"a step failed: {[s for s in report['steps'] if not s['ok']]}"
    # real work, not a successful-looking no-op
    for step in fixer_steps:
        assert step["actions"], f"{step['id']} produced no actions at all"

    post = run_gates(ws)
    assert post["durations"] == "ok", f"durations gate: {post['durations']}"
    assert post["normalise"] == "ok", f"normalise gate: {post['normalise']}"
    assert post["whole"] == "ok", f"whole suite: {post['whole']}"

    assert spent <= ceiling, f"smoke run spent {spent}, ceiling is {ceiling}"
    assert spent < 120, f"brief target is <120 requests for such a run, spent {spent}"


@pytest.mark.skipif(not LIVE, reason="set SMOKE_LIVE=1 to spend real requests")
def test_live_parallel_fixers_both_act(tmp_path):
    """The specific brief requirement: 2 fixers in parallel, both effective."""
    ws = fresh_workspace(tmp_path)
    team = Team(ws, max_requests=60)
    results = team.map_agents(EXPECTED_TASKS[:2])
    assert len(results) == 2
    assert [r.id for r in results] == ["A", "B"]
    assert all(r.ok for r in results), [r.error for r in results]
    assert all(r.actions for r in results), "a fixer did nothing"
    assert run_gates(ws)["durations"] == "ok"
    assert run_gates(ws)["normalise"] == "ok"


@pytest.mark.skipif(not LIVE, reason="set SMOKE_LIVE=1 to spend real requests")
def test_live_dependent_task_waits(tmp_path):
    """Task C depends on A and B; it must run last and see a green repo."""
    ws = fresh_workspace(tmp_path)
    team = Team(ws, max_requests=60)
    order: list[str] = []
    real = team.run_task

    def traced(task):
        result = real(task)
        order.append(task.id)
        return result

    team.run_task = traced
    team.map_agents(EXPECTED_TASKS)
    assert order[-1] == "C", f"dependent task must run last, got {order}"
    assert run_gates(ws)["whole"] == "ok"
