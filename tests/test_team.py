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
                           assert_disjoint, check_prompt_stability, compose_prompt,
                           status_file_for)

# ------------------------------------------------------------------- tasks
def test_task_render_always_ends_with_the_self_wake_line():
    task = Task(id="T1", role="fixer", instruction="Do the thing.")
    rendered = task.render()
    assert "TASK-Z:" in rendered, "self-wake line must be present"
    assert rendered.endswith(TASK_Z_LINE.replace("DONE.md", task.status_file))


def test_self_wake_names_a_per_task_file_not_a_shared_one():
    """Parallel fixers must not race on one inode.

    Found the hard way in the first live smoke run: both fixers were told to
    write DONE.md, and one found it occupied and appended around the other's
    status instead of owning its own report.
    """
    a, b = Task("T1", "fixer", "x"), Task("T2", "fixer", "y")
    assert a.status_file != b.status_file
    assert a.status_file == "DONE-T1.md" and b.status_file == "DONE-T2.md"
    assert "DONE-T1.md" in a.render() and "DONE-T2.md" in b.render()


def test_status_file_names_are_path_safe():
    """Task ids come from model output, so they must not escape the workspace.

    What matters is that no path separator or whitespace survives sanitising; a
    literal '..' left inside a flat filename is inert once the slashes are gone.
    """
    import os

    workspace = Path("/tmp/ws")
    for evil in ("../../etc/passwd", "a b c", "T1/T2", "$HOME", "..", ""):
        name = status_file_for(evil)
        assert os.sep not in name and "/" not in name and " " not in name
        assert name.startswith("DONE-") and name.endswith(".md")
        # the resolved path must stay inside the workspace
        resolved = (workspace / name)
        assert str(resolved).startswith(str(workspace))
        assert not name.startswith("."), name


def test_status_file_for_is_deterministic_and_unique():
    assert status_file_for("T1") == status_file_for("T1")
    assert status_file_for("T1") != status_file_for("T2")
    assert status_file_for("") == "DONE-task.md", "empty id still yields a safe name"


def test_self_wake_shared_sentence_is_byte_identical_across_tasks():
    """The TASK-Z sentence must be byte-identical apart from the filename.

    The instruction itself legitimately differs per task; the claim under test is
    narrower and more important: the self-wake text we append to EVERY task must
    not vary, or it silently busts the prompt cache (rule 1). Compared here by
    slicing out just the TASK-Z line.
    """
    def wake_line(text: str) -> str:
        return text[text.index("TASK-Z: "):]

    a, b = Task("T1", "fixer", "x").render(), Task("T2", "fixer", "y").render()
    tail = (" in this workspace and call finish anyway - the next agent wakes on "
            "that file, not on you.")
    pre = ("TASK-Z: if you get blocked, cannot proceed, or finish, still write "
           "your status to ")
    line_a, line_b = wake_line(a), wake_line(b)
    assert line_a == pre + "DONE-T1.md" + tail
    assert line_b == pre + "DONE-T2.md" + tail
    # identical on both sides of the only thing that varies
    head = ".md"
    assert line_a[:line_a.index("DONE-")] == line_b[:line_b.index("DONE-")]
    assert line_a[line_a.index(head) + len(head):] == tail
    assert line_b[line_b.index(head) + len(head):] == tail
    # and a third task keeps the same shape
    c = wake_line(Task("T3", "tester", "z", paths=("p.py",)).render())
    assert c == pre + "DONE-T3.md" + tail


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
