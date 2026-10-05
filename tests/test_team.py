"""Offline tests for team/run_team.py (T6 gate). Zero API calls.

The orchestrator's job is to make an expensive failure cheap, so most of these
assert that something is REFUSED before any request is sent — a ceiling, an
overlap, an unstable prompt, a role file that parses to no tools.

No test here constructs a Conversation and runs it: that costs real quota and
belongs in tests/test_e2e_smoke.py behind an explicit opt-in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from team.run_team import (TASK_Z_LINE, Task, Team, TeamError,  # noqa: E402
                           assert_disjoint, check_prompt_stability, compose_prompt)

# ------------------------------------------------------------------- tasks
def test_task_render_always_ends_with_the_self_wake_line():
    task = Task(id="T1", role="fixer", instruction="Do the thing.")
    assert task.render().endswith(TASK_Z_LINE), "self-wake line must be present"
    # and it is the SAME bytes for every task: a varying suffix would break the
    # per-turn cache prefix for no benefit
    other = Task(id="T2", role="tester", instruction="Something else.",
                 paths=("a.py",), acceptance=("pytest -q",)).render()
    assert other.endswith(TASK_Z_LINE)
    assert other[len(other) - len(TASK_Z_LINE):] == TASK_Z_LINE


def test_task_render_lists_paths_and_gates():
    text = Task(id="T1", role="fixer", instruction="fix it",
                paths=("a.py", "b.py"),
                acceptance=("pytest -q tests/test_a.py",)).render()
    assert "a.py, b.py" in text
    assert "pytest -q tests/test_a.py" in text
    assert "Acceptance gates" in text


def test_task_render_does_not_execute_gates():
    """A gate string must stay inert data, never a shell command here."""
    evil = Task(id="T1", role="fixer", instruction="x",
                acceptance=("rm -rf / #",))
    assert "rm -rf" in evil.render()          # reported verbatim to the agent
    # and nothing in this module imports subprocess/subprocess-exec for gates
    src = (REPO / "team" / "run_team.py").read_text(encoding="utf-8")
    assert "import subprocess" not in src, "orchestrator must not shell out to gates"
    assert "os.system" not in src


# ---------------------------------------------------------------- stability
def test_prompt_stability_rejects_a_timestamp():
    with pytest.raises(TeamError, match="not byte-stable"):
        check_prompt_stability("You are an agent. Today is 2026-10-05T08:00.")


def test_prompt_stability_rejects_uuid_run_id_and_hex_blob():
    for bad in ("run abcdef01-2345-6789-abcd-ef0123456789 here",
                "task_id 42 matters",
                "prefix deadbeefdeadbeefdeadbeef01"):
        with pytest.raises(TeamError, match="not byte-stable"):
            check_prompt_stability(bad)


def test_prompt_stability_accepts_fixed_text():
    check_prompt_stability("You are the ARCHITECT. Read files, never write code.")
    check_prompt_stability("")           # empty is stable, not unstable


def test_compose_prompt_checks_the_role_body():
    class D:
        name = "fixer"
        system_prompt = "Do it. Built at 14:30 UTC."

    with pytest.raises(TeamError, match="not byte-stable"):
        compose_prompt(D())


def test_shipped_role_prompts_are_cache_stable():
    """The files we ship must not contain anything that busts their own cache."""
    team = Team(REPO)
    for name, defn in team.defs.items():
        compose_prompt(defn)  # raises if unstable


# ------------------------------------------------------------------ overlap
def test_disjoint_overlap_is_refused():
    tasks = [Task("T1", "fixer", "a", paths=("src/main.py",)),
             Task("T2", "fixer", "b", paths=("src/main.py",))]
    with pytest.raises(TeamError, match="both claim"):
        assert_disjoint(tasks)


def test_disjoint_paths_are_allowed():
    assert_disjoint([Task("T1", "fixer", "a", paths=("src/a.py",)),
                     Task("T2", "fixer", "b", paths=("src/b.py",))])


def test_same_task_repeating_a_path_is_not_an_overlap():
    assert_disjoint([Task("T1", "fixer", "a", paths=("x.py", "x.py"))])


def test_tasks_without_paths_are_parallel_safe():
    assert_disjoint([Task("T1", "fixer", "a"), Task("T2", "fixer", "b")])
