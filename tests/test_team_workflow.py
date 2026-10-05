"""Offline tests for the orchestrator's spec parsing, workflow, and CLI. Zero spend.

Split from tests/test_team.py (which covers Task rendering, prompt stability, and
the disjoint-path rule) so the two files stay readable and never collide.

Nothing here constructs a Conversation and runs it: that costs quota and belongs
behind the explicit opt-in in tests/test_e2e_smoke.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from team.run_team import (AgentResult, Task, Team, TeamError, assert_disjoint,  # noqa: E402
                           main, parse_tasks)

# ----------------------------------------------------------- spec parsing
SPEC = """
Here is my plan.

```json
[
  {"id": "T1", "role": "fixer", "instruction": "Fix add()",
   "paths": ["src/math.py"], "acceptance": ["pytest -q tests/test_math.py"]},
  {"id": "T2", "role": "tester", "instruction": "Add coverage",
   "paths": ["tests/test_math.py"], "acceptance": ["pytest -q"]}
]
```
"""


def test_parse_tasks_reads_a_fenced_json_array():
    tasks = parse_tasks(SPEC)
    assert len(tasks) == 2
    assert tasks[0].id == "T1" and tasks[0].role == "fixer"
    assert tasks[0].paths == ("src/math.py",)
    assert tasks[0].acceptance == ("pytest -q tests/test_math.py",)
    assert tasks[1].id == "T2"


def test_parse_tasks_accepts_a_tasks_wrapper():
    tasks = parse_tasks('```json\n{"tasks": [{"id": "A", "role": "fixer",'
                        ' "instruction": "x"}]}\n```')
    assert len(tasks) == 1 and tasks[0].id == "A"


def test_parse_tasks_finds_a_bare_array_among_prose():
    tasks = parse_tasks('spec: [{"instruction": "do it", "role": "fixer"}] done')
    assert len(tasks) == 1


def test_parse_tasks_returns_empty_on_garble_not_a_half_read_spec():
    """A silently wrong task list is worse than no task list."""
    assert parse_tasks("no json here at all") == []
    assert parse_tasks("```json\n[{") == []
    assert parse_tasks("") == []
    assert parse_tasks("```json\n[42, 7]\n```") == []


def test_parse_tasks_defaults_missing_fields():
    tasks = parse_tasks('[{"instruction": "only this"}]')
    assert tasks[0].role == "fixer" and tasks[0].id == "T1"
    assert tasks[0].paths == () and tasks[0].acceptance == ()


def test_parsed_spec_is_overlap_checkable():
    tasks = parse_tasks(SPEC)
    assert_disjoint(tasks)                        # these two are parallel-safe
    tasks.append(Task("T3", "fixer", "clash", paths=("src/math.py",)))
    with pytest.raises(TeamError, match="both claim"):
        assert_disjoint(tasks)


# ---------------------------------------------------------- Team wiring
def test_team_constructs_with_zero_requests():
    team = Team(REPO)
    assert team._requests == 0
    assert {"architect", "fixer", "reviewer", "tester"} <= set(team.defs)


def test_team_rejects_a_missing_workspace():
    with pytest.raises(TeamError, match="does not exist"):
        Team("/no/such/workspace/anywhere")


def test_team_rejects_ceiling_above_configured():
    with pytest.raises(TeamError, match="exceeds the"):
        Team(REPO, max_requests=999999)


def test_ceiling_blocks_before_the_spend_not_after():
    team = Team(REPO, max_requests=3)
    team._charge(); team._charge(); team._charge()
    with pytest.raises(TeamError, match="would exceed"):
        team._charge()
    assert team._requests == 3, "refusal must not increment the counter"


def test_build_agent_proves_every_role_can_act():
    """The guard that would have saved the netflow run, wired into the team."""
    team = Team(REPO)
    for name in team.defs:
        agent, _llm, lane = team.build_agent(name)
        names = {getattr(t, "name", str(t)) for t in agent.tools}
        assert names, f"{name} has no tools"
        assert not names <= {"finish", "think"}, f"{name} is crippled (RC1)"
        assert lane.model.count("/") == 1


def _roles_dir(tmp_path: Path, architect_text: str) -> Path:
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "architect.md").write_text(architect_text, encoding="utf-8")
    for role in ("fixer", "reviewer", "tester"):
        (roles / f"{role}.md").write_text(
            "---\nname: " + role + "\ndescription: d\nmodel: inherit\n"
            "tools:\n  - terminal\n---\nbody\n", encoding="utf-8")
    return roles


def test_a_toolless_role_is_rejected_at_construction(tmp_path):
    """RC1 regression, and stronger than I first asserted: the rejection happens
    when the Team is built, before any agent is even constructed. Nothing can
    spend a request against a role that could not act."""
    roles = _roles_dir(tmp_path, "---\nname: architect\ndescription: d\n"
                                "model: inherit\ntools: []\n---\nARCHITECT.\n")
    with pytest.raises(TeamError) as exc:
        Team(REPO, roles_dir=roles)
    message = str(exc.value)
    assert "role files rejected" in message
    assert "no tool call and no content" in message, \
        "the error must name the failure mode it is preventing"
    assert "architect.md" in message, "the error must name the offending file"


def test_build_agent_repairs_a_leading_title_role_file(tmp_path):
    """A role in the historically broken shape is normalised AND warned about.

    Repairing silently would be its own kind of RC1: the file on disk is still
    wrong, and the next tool that reads it directly gets no tools at all.
    """
    import warnings

    roles = _roles_dir(tmp_path, "# architect\n---\nname: architect\n"
                                 "description: d\nmodel: inherit\ntools:\n"
                                 "  - terminal\n  - file_editor\n---\nARCHITECT.\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        team = Team(REPO, roles_dir=roles)
    agent, _llm, _lane = team.build_agent("architect")
    names = {getattr(t, "name", str(t)) for t in agent.tools}
    assert {"terminal", "file_editor"} <= names
    assert any("frontmatter was not on line 1" in str(w.message) for w in caught), \
        "the repair must be warned about, not done silently"


def test_map_agents_refuses_overlapping_tasks_before_spending():
    team = Team(REPO)
    tasks = [Task("T1", "fixer", "a", paths=("same.py",)),
             Task("T2", "fixer", "b", paths=("same.py",))]
    before = team._requests
    with pytest.raises(TeamError, match="both claim"):
        team.map_agents(tasks)
    assert team._requests == before, "overlap check must precede any charge"


def test_map_agents_on_empty_list_is_a_noop():
    assert Team(REPO).map_agents([]) == []


def test_conversation_kwargs_are_factory_safe():
    from inspect import signature

    from openhands.sdk import Conversation

    params = set(signature(Conversation.__new__).parameters)
    team = Team(REPO)
    for name in team.defs:
        kwargs = team.router.public_conversation_kwargs(team.role_for(name))
        for key in kwargs:
            assert key in params, f"{name}: {key} rejected by the factory"


# ------------------------------------------------------------- workflow
def test_run_workflow_reports_architect_failure(monkeypatch):
    team = Team(REPO)
    monkeypatch.setattr(team, "run_task", lambda task: AgentResult(
        id=task.id, role=task.role, ok=False, error="boom"))
    report = team.run_workflow(REPO / "router" / "router.yaml")
    assert "boom" in report.get("aborted", "")


def test_run_workflow_warns_when_the_spec_is_unparseable(monkeypatch):
    team = Team(REPO)
    calls: list[str] = []

    def fake_run(task):
        calls.append(task.id)
        return AgentResult(id=task.id, role=task.role, ok=True, requests=1,
                           text="A lovely prose plan with no JSON block.")

    monkeypatch.setattr(team, "run_task", fake_run)
    report = team.run_workflow(REPO / "router" / "router.yaml")
    assert "spec" in calls and "review" in calls
    assert report["tasks_parsed"] == 0
    assert any("no parseable task block" in w for w in report.get("warnings", [])), \
        "an unparsed spec must be surfaced, not silently skipped"


def test_run_workflow_runs_fixers_when_the_spec_parses(monkeypatch):
    team = Team(REPO)
    calls: list[str] = []

    def fake_run(task):
        calls.append(task.id)
        text = SPEC if task.id == "spec" else "done"
        return AgentResult(id=task.id, role=task.role, ok=True, requests=1,
                           text=text)

    monkeypatch.setattr(team, "run_task", fake_run)
    report = team.run_workflow(REPO / "router" / "router.yaml")
    assert report["tasks_parsed"] == 2
    assert calls.count("T1") == 1 and calls.count("T2") == 1
    assert "review" in calls


def test_run_workflow_never_puts_agent_text_in_the_report(monkeypatch):
    """Reports get pasted into chat; agent text can quote a credential."""
    secret = "sk-" + "Qz7RbN3Td" + "XkV8Mw2p"          # assembled at runtime
    team = Team(REPO)

    def fake_run(task):
        return AgentResult(id=task.id, role=task.role, ok=True, requests=1,
                           text="quoting " + secret + " from a file")

    monkeypatch.setattr(team, "run_task", fake_run)
    report = team.run_workflow(REPO / "router" / "router.yaml")
    blob = json.dumps(report, default=str)
    assert secret not in blob, "the report echoed agent text"
    assert report["steps"][0]["text_len"] > 0, "length is reported, content is not"


# ------------------------------------------------------------------ CLI
def test_cli_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_cli_lanes_lists_models_at_zero_cost(capsys):
    assert main(["--lanes"]) == 0
    out = capsys.readouterr().out
    assert "anthropic/glm-5.3" in out and "qwen3.8-flash" in out
    assert "run_requests_ceiling" in out


def test_cli_plan_validates_all_roles_without_spend(capsys):
    assert main(["--plan", "--brief", str(REPO / "pyproject.toml")]) == 0
    out = capsys.readouterr().out
    for role in ("architect", "fixer", "reviewer", "tester"):
        assert role in out
    assert "PLAN OK" in out and "0 requests spent" in out


def test_cli_plan_flags_an_unreadable_brief(capsys):
    assert main(["--plan", "--brief", "/no/such/brief.md"]) == 1
    out = capsys.readouterr().out
    assert "readable=False" in out and "WARNINGS" in out


def test_cli_refuses_to_run_without_a_brief():
    assert main(["--workspace", str(REPO)]) == 2


def test_cli_refuses_a_nonexistent_brief():
    assert main(["--brief", "/no/such/brief.md", "--workspace", str(REPO)]) == 2


def test_cli_rejects_a_ceiling_above_config(capsys):
    assert main(["--plan", "--brief", str(REPO / "pyproject.toml"),
                 "--max-requests", "99999"]) == 2
    assert "REFUSED BEFORE SPEND" in capsys.readouterr().err
