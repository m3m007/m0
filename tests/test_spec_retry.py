"""Offline tests for spec-step validation, the retry, and the stale-file guard.

Why these exist. Two live e2e runs failed identically: the architect wrote a
TEAM-SPEC.json, ended its turn with a handful of characters of reply, and the
flow fell through file-then-reply into "architect produced no parseable task
spec; fixers were skipped" while the reviewer went on grading a repo nobody had
touched. Loud was not enough: the step needed a real check and one second chance.

Zero spend, zero network. Every test stubs ``Team.run_task`` (the style of
tests/test_runrecord.py) with a fake that spends through ``team._charge()`` /
``team._settle()`` exactly as the real one does, so request totals are asserted
against the same ceiling the live path enforces rather than against a number a
stub invented.

Workspaces are always tmp_path: run_workflow() writes TEAM-SPEC.json into
self.workspace, and a file left behind by one test would silently answer for
another.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from _pytest.outcomes import Failed

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from team.run_team import AgentResult, Task, Team, TeamError  # noqa: E402
from tests.test_e2e_smoke import SYSTEM_PYTHON, assert_live_interpreter  # noqa: E402

#: A two-task plan, as it would sit in the spec file.
SPEC_JSON = json.dumps([
    {"id": "T1", "role": "fixer", "instruction": "Fix durations.",
     "paths": ["pkg/durations.py"],
     "acceptance": ["python3 -m pytest -q tests/test_durations.py"]},
    {"id": "T2", "role": "fixer", "instruction": "Fix normalise.",
     "paths": ["pkg/normalise.py"],
     "acceptance": ["python3 -m pytest -q tests/test_normalise.py"]},
])

#: The same array as an architect reply, so file and reply can be swapped in
#: isolation while the plan they describe stays identical.
SPEC_REPLY = "plan:\n\n```json\n" + SPEC_JSON + "\n```\n"

#: A spec file that parses fine but describes someone else's work: valid JSON,
#: valid tasks, and not this run's plan.
STALE_PAYLOAD = json.dumps([{"id": "OLD", "role": "fixer",
                             "instruction": "Re-run last week's plan.",
                             "paths": ["old/thing.py"]}])


def backdate(path: Path, age: float = 3600.0) -> Path:
    """Move a file's timestamps into the past so it provably predates the step."""
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def spec_step(text: str = "", payload: str | None = None, *,
              spec_path: Path | None = None, mtime: float | None = None,
              completions: int = 1):
    """One architect attempt: write the file (if asked), then reply with text."""
    def step(team: Team, task: Task) -> AgentResult:
        if payload is not None:
            assert spec_path is not None, "this step needs a path to write to"
            spec_path.write_text(payload, encoding="utf-8")
            if mtime is not None:
                os.utime(spec_path, (mtime, mtime))
        return AgentResult(id=task.id, role=task.role, ok=True,
                           requests=completions, text=text)
    return step


def scripted_team(tmp_path, monkeypatch, steps: list, *,
                  ceiling: int | None = None) -> tuple[Team, list[str]]:
    """A Team whose run_task replays ``steps`` for the spec step, and charges.

    The fake spends through ``_charge``/``_settle`` like the real run_task, so a
    retry pays for itself and an over-ceiling retry raises the same TeamError
    the live path raises, before the step reaches any wire.
    """
    team = Team(tmp_path, max_requests=ceiling)
    remaining = list(steps)
    calls: list[str] = []

    def fake_run(task: Task) -> AgentResult:
        team._charge()
        calls.append(task.id)
        if task.id == "spec":
            assert remaining, "a third spec step was sent; the retry is capped at one"
            result = remaining.pop(0)(team, task)
            team._settle(result.requests)
            return result
        return AgentResult(id=task.id, role=task.role, ok=True, requests=1)

    monkeypatch.setattr(team, "run_task", fake_run)
    return team, calls


# ------------------------------------------------------- file is the primary
def test_a_valid_spec_file_is_used_without_a_retry(monkeypatch, tmp_path):
    team, calls = scripted_team(
        tmp_path, monkeypatch,
        [spec_step(payload=SPEC_JSON, spec_path=tmp_path / "TEAM-SPEC.json")])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_source"] == "file"
    assert report["spec_attempts"] == 1, "a good file must not cost a second request"
    assert report["spec_validation"] == {"file_exists": True, "file_tasks": 2,
                                         "file_reason": "valid", "reply_tasks": 0}
    assert report["tasks_parsed"] == 2
    assert not report.get("warnings")
    assert calls == ["spec", "T1", "T2", "review"]


def test_the_file_beats_a_reply_that_disagrees_with_it(monkeypatch, tmp_path):
    """File first, always: the reply is a fallback, not a tie-breaker."""
    team, _calls = scripted_team(
        tmp_path, monkeypatch,
        [spec_step(text=SPEC_REPLY, payload=SPEC_JSON,
                   spec_path=tmp_path / "TEAM-SPEC.json")])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_source"] == "file"
    assert report["spec_validation"]["reply_tasks"] == 2, "the reply is still recorded"
    assert [s["id"] for s in report["steps"]] == ["spec", "T1", "T2", "review"]


# --------------------------------------------------------- reply is the fallback
def test_a_reply_is_used_when_there_is_no_spec_file(monkeypatch, tmp_path):
    team, calls = scripted_team(tmp_path, monkeypatch, [spec_step(text=SPEC_REPLY)])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_source"] == "reply"
    assert report["spec_attempts"] == 1, "the reply is a success, not a reason to retry"
    assert report["spec_validation"] == {"file_exists": False, "file_tasks": 0,
                                         "file_reason": "missing", "reply_tasks": 2}
    assert calls == ["spec", "T1", "T2", "review"]


def test_an_unparseable_file_falls_back_to_the_reply(monkeypatch, tmp_path):
    team, calls = scripted_team(
        tmp_path, monkeypatch,
        [spec_step(text=SPEC_REPLY, payload="{not json",
                   spec_path=tmp_path / "TEAM-SPEC.json")])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_source"] == "reply"
    assert report["spec_attempts"] == 1
    assert report["spec_validation"]["file_exists"] is True
    assert report["spec_validation"]["file_reason"] == "unparseable"
    assert any("spec file present but unparseable" in w
               for w in report.get("warnings", [])), "the ignored file is surfaced"
    assert calls == ["spec", "T1", "T2", "review"], "no broken task may run"


def test_a_file_that_holds_no_tasks_is_not_a_plan(monkeypatch, tmp_path):
    """Valid JSON that contains no tasks is not a plan, so the step is re-asked."""
    team, calls = scripted_team(
        tmp_path, monkeypatch,
        [spec_step(payload="[]", spec_path=tmp_path / "TEAM-SPEC.json"),
         spec_step(text=SPEC_REPLY)])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_validation"] == {"file_exists": True, "file_tasks": 0,
                                         "file_reason": "no-tasks", "reply_tasks": 2}
    assert report["spec_attempts"] == 2, "a zero-task file is a failed spec step"
    assert report["spec_source"] == "reply"
    assert any("spec file present but no-tasks" in w for w in report["warnings"])
    assert calls == ["spec", "spec", "T1", "T2", "review"]


def test_a_zero_byte_file_reports_empty(monkeypatch, tmp_path):
    team, _calls = scripted_team(
        tmp_path, monkeypatch,
        [spec_step(text=SPEC_REPLY, payload="",
                   spec_path=tmp_path / "TEAM-SPEC.json")])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_validation"]["file_reason"] == "empty"


# ------------------------------------------------------------------- the retry
def test_a_dead_spec_step_is_retried_once_and_then_runs(monkeypatch, tmp_path):
    """The exact live failure: ~4 characters of reply, nothing on disk."""
    team, calls = scripted_team(
        tmp_path, monkeypatch,
        [spec_step(text="ok"),
         spec_step(payload=SPEC_JSON, spec_path=tmp_path / "TEAM-SPEC.json")])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_attempts"] == 2
    assert report["spec_source"] == "file"
    assert report["tasks_parsed"] == 2
    assert report["spec_validation"]["file_tasks"] == 2
    assert calls == ["spec", "spec", "T1", "T2", "review"]
    assert not report.get("warnings"), "a recovered spec step must not also warn"


def test_a_reply_on_the_retry_is_enough(monkeypatch, tmp_path):
    team, _calls = scripted_team(tmp_path, monkeypatch,
                                 [spec_step(text="ok"), spec_step(text=SPEC_REPLY)])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_attempts"] == 2
    assert report["spec_source"] == "reply"
    assert report["tasks_parsed"] == 2
    assert report["spec_validation"] == {"file_exists": False, "file_tasks": 0,
                                        "file_reason": "missing", "reply_tasks": 2}


def test_both_attempts_empty_warns_and_still_runs_the_reviewer(monkeypatch,
                                                              tmp_path):
    team, calls = scripted_team(tmp_path, monkeypatch,
                                [spec_step(text="A lovely prose plan, no JSON."),
                                 spec_step(text="done")])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_attempts"] == 2
    assert report["spec_source"] is None
    assert report["tasks_parsed"] == 0
    assert any("no parseable task spec" in w for w in report.get("warnings", [])), \
        "the existing loud warning is still the outcome of a double failure"
    assert calls == ["spec", "spec", "review"], "fixers skipped, reviewer still runs"
    assert report["requests_charged"] == 3 == team._requests
    assert report["spec_requests_spent"] == [1, 1]


def test_retry_requests_are_the_ones_actually_charged(monkeypatch, tmp_path):
    """spec_requests_spent is read off the team's books, not the stub's claim.

    The fake attempt settles 3 real completions while reserving 1, so the team's
    own counter is what the report must show: 3 per attempt, and the run total
    equals 3 + 3 for the two spec attempts plus one request each for the two
    fixers and the reviewer.
    """
    team, calls = scripted_team(tmp_path, monkeypatch,
                                [spec_step(text="ok", completions=3),
                                 spec_step(text=SPEC_REPLY, completions=3)])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_attempts"] == 2
    assert report["spec_requests_spent"] == [3, 3]
    assert report["requests_charged"] == team._requests
    assert report["requests_charged"] == 3 + 3 + 1 + 1 + 1
    assert calls == ["spec", "spec", "T1", "T2", "review"]


def test_the_retry_pays_for_itself_against_the_ceiling(monkeypatch, tmp_path):
    """A retry is a step, not a free second chance: it can be refused for cost."""
    team, calls = scripted_team(tmp_path, monkeypatch, [spec_step(text="ok")],
                                ceiling=1)
    with pytest.raises(TeamError, match="ceiling"):
        team.run_workflow(tmp_path / "BRIEF.md")
    assert calls == ["spec"], "the retry never reached the wire"
    assert team._requests == 1


def test_both_spec_attempts_are_kept_in_the_step_list(monkeypatch, tmp_path):
    team, _calls = scripted_team(tmp_path, monkeypatch,
                                 [spec_step(text="ok"), spec_step(text="ok")])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    spec_steps = [s for s in report["steps"] if s["id"] == "spec"]
    assert len(spec_steps) == 2, "each attempt is its own step, for the audit"
    assert report["spec_attempts"] == 2
    assert report["spec_validation"]["reply_tasks"] == 0, "last attempt is reported"


def test_a_failed_architect_still_reports_the_validation(monkeypatch, tmp_path):
    """spec_validation is recorded even when the step itself errors out."""
    team, calls = scripted_team(tmp_path, monkeypatch,
                                [lambda tm, task: AgentResult(
                                    id=task.id, role=task.role, ok=False,
                                    requests=1, error="boom")])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert "boom" in report["aborted"], "the abort path is unchanged"
    assert calls == ["spec"], "a failed architect runs nothing else"
    assert report["spec_attempts"] == 1
    assert set(report["spec_validation"]) == {"file_exists", "file_tasks",
                                              "file_reason", "reply_tasks"}
    assert report["spec_validation"]["file_exists"] is False
    assert report["spec_requests_spent"] == [1]
    assert report["requests_charged"] == 1 == team._requests


# --------------------------------------------------------- stale spec files
def test_a_pre_existing_spec_file_cannot_supply_the_fresh_plan(monkeypatch,
                                                              tmp_path):
    """A stale TEAM-SPEC.json is a silent wrong plan; it must never be run.

    The file holds valid JSON with valid tasks, so the only way it could be used
    is by trusting a plan this architect never wrote. The architect replies
    nothing on either attempt, so the run must end with zero tasks rather than
    with last week's tasks.
    """
    spec = tmp_path / "TEAM-SPEC.json"
    spec.write_text(STALE_PAYLOAD, encoding="utf-8")
    backdate(spec)
    team, calls = scripted_team(tmp_path, monkeypatch,
                                [spec_step(text=""), spec_step(text="")])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_validation"] == {"file_exists": False, "file_tasks": 0,
                                         "file_reason": "stale", "reply_tasks": 0}
    assert report["spec_attempts"] == 2, "stale contents are no plan: retry once"
    assert report["tasks_parsed"] == 0
    assert report["spec_source"] is None
    assert [s["id"] for s in report["steps"]] == ["spec", "spec", "review"]
    assert calls == ["spec", "spec", "review"], "no stale task may run"
    assert any("stale" in w for w in report.get("warnings", []))
    assert any("no parseable task spec" in w for w in report.get("warnings", []))
    assert report["requests_charged"] == 3 == team._requests


def test_a_stale_file_is_overridden_once_the_architect_actually_writes(monkeypatch,
                                                                      tmp_path):
    """The guard must cost a working architect nothing but the one re-ask."""
    spec = tmp_path / "TEAM-SPEC.json"
    spec.write_text(STALE_PAYLOAD, encoding="utf-8")
    backdate(spec)
    team, calls = scripted_team(tmp_path, monkeypatch,
                                [spec_step(text="ok"),
                                 spec_step(payload=SPEC_JSON, spec_path=spec)])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_source"] == "file"
    assert report["spec_attempts"] == 2
    assert report["spec_validation"]["file_reason"] == "valid"
    assert "OLD" not in json.dumps(report["spec_validation"])
    assert calls == ["spec", "spec", "T1", "T2", "review"]


def test_a_rewrite_with_a_blunted_mtime_is_not_called_stale(monkeypatch, tmp_path):
    """Coarse timestamps must not defeat a real write.

    The architect overwrites a file that predates the step and the filesystem is
    too blunt to advance mtime. The bytes changed, so this IS the run's plan;
    judging by mtime alone would throw a good spec away.
    """
    spec = tmp_path / "TEAM-SPEC.json"
    spec.write_text(STALE_PAYLOAD, encoding="utf-8")
    old = time.time() - 3600
    os.utime(spec, (old, old))
    team, _calls = scripted_team(
        tmp_path, monkeypatch,
        [spec_step(text="ok"), spec_step(payload=SPEC_JSON, spec_path=spec,
                                         mtime=old)])
    report = team.run_workflow(tmp_path / "BRIEF.md")
    assert report["spec_validation"]["file_reason"] == "valid"
    assert report["spec_source"] == "file"
    assert report["tasks_parsed"] == 2


def test_freshness_is_mtime_and_identity_not_just_either(tmp_path):
    """Direct unit test of the predicate: both halves have to agree."""
    team = Team(tmp_path)
    spec = tmp_path / "TEAM-SPEC.json"
    started = team._spec_start_stamp()
    assert team._spec_signature(spec) is None
    assert not team._spec_is_fresh(spec, started, None), "absent is not fresh"

    # last run's file: predates the step and survives it untouched -> stale
    spec.write_text(STALE_PAYLOAD, encoding="utf-8")
    backdate(spec)
    before = team._spec_signature(spec)
    assert not team._spec_is_fresh(spec, started, before), \
        "old and unchanged is stale"

    # the architect rewrote it during the step -> this run's plan, even with the
    # timestamp pushed back past the boundary: content changed, so it is not the
    # file the step started with
    spec.write_text(SPEC_JSON, encoding="utf-8")
    os.utime(spec, (spec.stat().st_mtime - 7200,) * 2)
    assert team._spec_is_fresh(spec, started, before), \
        "changed content is this run's work, whatever the mtime says"

    # a file written during the step and left alone is fresh by mtime alone
    spec.write_text(STALE_PAYLOAD, encoding="utf-8")
    now = team._spec_signature(spec)
    assert spec.stat().st_mtime >= started
    assert team._spec_is_fresh(spec, started, now), "a fresh write is fresh"


def test_the_start_stamp_is_comparable_with_mtime_and_never_moves_backwards():
    """Monotone, yet on wall time: an NTP step cannot un-stale a file."""
    team_class = Team
    first = team_class._spec_start_stamp()
    second = team_class._spec_start_stamp()
    assert second >= first
    assert abs(first - time.time()) < 60, "the stamp must sit on wall time"


def test_file_state_reason_codes(tmp_path):
    team = Team(tmp_path)
    spec = tmp_path / "TEAM-SPEC.json"
    started = team._spec_start_stamp()
    assert team._spec_file_state(spec, started, None) == ([], False, "missing")
    spec.write_text("{oops", encoding="utf-8")
    assert team._spec_file_state(spec, started, None) == ([], True, "unparseable")
    spec.write_text("[]", encoding="utf-8")
    assert team._spec_file_state(spec, started, None) == ([], True, "no-tasks")
    spec.write_text("", encoding="utf-8")
    assert team._spec_file_state(spec, started, None) == ([], True, "empty")
    spec.write_text(SPEC_JSON, encoding="utf-8")
    tasks, exists, reason = team._spec_file_state(spec, started, None)
    assert (exists, reason) == (True, "valid")
    assert [t.id for t in tasks] == ["T1", "T2"]


# ------------------------------------------------------- the interpreter guard
def test_the_guard_accepts_the_system_python(monkeypatch):
    monkeypatch.setenv("SMOKE_LIVE", "1")
    monkeypatch.delenv("MULTIAGENT_PY", raising=False)
    assert_live_interpreter(SYSTEM_PYTHON)
    assert_live_interpreter(str(Path(SYSTEM_PYTHON).resolve()))


def test_the_guard_refuses_a_non_system_python(monkeypatch, tmp_path):
    """The venv interpreter has no pytest: name the working command instead."""
    fake = tmp_path / "python-not-the-system-one"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("SMOKE_LIVE", "1")
    monkeypatch.delenv("MULTIAGENT_PY", raising=False)
    with pytest.raises(Failed) as exc:
        assert_live_interpreter(str(fake))
    message = str(exc.value)
    assert str(fake) in message
    assert ("SMOKE_LIVE=1 /usr/bin/python3 -m pytest tests/test_e2e_smoke.py -q -s"
            in message), message
    assert "\n" not in message.strip(), \
        "one line: a wrong interpreter is no stack trace"


def test_the_guard_honours_the_interpreter_override(monkeypatch, tmp_path):
    fake = tmp_path / "deliberately-not-system-python"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("SMOKE_LIVE", "1")
    monkeypatch.setenv("MULTIAGENT_PY", str(fake))
    assert_live_interpreter(str(fake))


def test_the_guard_is_inert_offline(monkeypatch, tmp_path):
    """The default suite runs on whatever python3 pytest happens to live in."""
    monkeypatch.delenv("SMOKE_LIVE", raising=False)
    monkeypatch.delenv("MULTIAGENT_PY", raising=False)
    assert_live_interpreter(str(tmp_path / "not-the-system-python"))


def test_the_smoke_header_documents_the_working_command():
    header = (REPO / "tests" / "test_e2e_smoke.py").read_text(encoding="utf-8")
    assert ("SMOKE_LIVE=1 /usr/bin/python3 -m pytest tests/test_e2e_smoke.py -q -s"
            in header), "the live command must be stated, not implied"


def test_the_guard_fires_at_collection_time(monkeypatch, tmp_path):
    """The module-level call, so a wrong interpreter stops the run pre-spend.

    Executed in a throwaway module: importlib.reload would clear the dict of
    the already-imported test_e2e_smoke and break its functions' globals for
    any test that runs after this one.
    """
    import importlib.util

    fake = tmp_path / "python-from-the-oh-venv"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("SMOKE_LIVE", "1")
    monkeypatch.delenv("MULTIAGENT_PY", raising=False)
    monkeypatch.setattr(sys, "executable", str(fake))

    spec = importlib.util.spec_from_file_location(
        "smoke_module_under_test", REPO / "tests" / "test_e2e_smoke.py")
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(Failed) as exc:
        spec.loader.exec_module(module)
    message = str(exc.value)
    assert ("/usr/bin/python3 -m pytest tests/test_e2e_smoke.py -q -s" in message), \
        message
    assert str(fake) in message, "the message names the interpreter that is wrong"


def test_the_readme_documents_the_same_command():
    """Doc drift is how the next person burns an hour on a wrong interpreter."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert ("SMOKE_LIVE=1 $PY -m pytest tests/test_e2e_smoke.py -q -s" in readme)
    assert "PY=/usr/bin/python3" in readme
    assert 'V=<venv-with-openhands-sdk>/bin/python' in readme
