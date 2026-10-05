"""team.run_team — orchestrator: architect -> parallel fixers -> reviewer.

Built on top of router/ and fix/oh_compat, so every lane is probe-checked, every
sub-agent is proven runnable BEFORE it can spend a request, and every request is
counted against a per-lane budget.

    python3 team/run_team.py --plan  --brief PATH            # 0 calls: validate
    python3 team/run_team.py --brief PATH --max-requests 60  # run for real
    python3 team/run_team.py --help

Three choices that are load-bearing rather than cosmetic:

* PARALLEL ONLY WHERE DISJOINT. ``map_agents`` refuses to run two tasks whose
  declared file sets overlap. A torn write is invisible in the final test output
  and expensive to debug, so the overlap check happens before anything is spent.

* PROMPT PREFIX STABILITY IS ENFORCED, NOT DOCUMENTED. Rule 1 of the cache
  contract requires a byte-stable system prompt, so ``check_prompt_stability``
  rejects a prompt containing a timestamp, run id, or long hex blob. Those
  guarantee a cache miss on every turn, which on this plan means paying full
  price for every single request.

* TASK-Z SELF-WAKE. Every task instruction ends with the same literal line
  (``TASK_Z_LINE``), so a stalled agent re-enters the queue by writing DONE.md
  instead of waiting for a supervisor that is not coming. It is identical for
  every task, so it costs nothing in cache terms.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from fix.oh_compat import (RoleFileError, assert_runnable, ensure_tools_registered,  # noqa: E402
                           load_roles, spawn_agent)
from router import Router, RouterError  # noqa: E402

ROLES_DIR = HERE / "roles"

#: The Task-Z self-wake line, appended verbatim to every task.
TASK_Z_LINE = (
    "TASK-Z: if you get blocked, cannot proceed, or finish, still write your "
    "status to DONE.md in this workspace and call finish anyway - the next "
    "agent wakes on that file, not on you.")

#: Prompt fragments that make a cache hit impossible.
UNSTABLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iso-datetime", re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")),
    ("epoch-like number", re.compile(r"\b1[7-9]\d{8}\b")),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                        r"[0-9a-f]{12}\b", re.I)),
    ("long hex blob", re.compile(r"\b[0-9a-f]{24,}\b", re.I)),
    ("runtime id", re.compile(r"\b(?:pid|thread[ _-]?id|run[ _-]?id|task[ _-]?id)\b",
                             re.I)),
    ("time of day", re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm|UTC|GMT)\b", re.I)),
)


class TeamError(RuntimeError):
    """Refused before spending anything: bad config, overlap, or unstable prompt."""


# --------------------------------------------------------------- data model
@dataclass
class Task:
    """One unit of delegated work."""

    id: str
    role: str
    instruction: str
    paths: tuple[str, ...] = ()        # files this task may touch
    acceptance: tuple[str, ...] = ()   # commands that must each exit 0
    depends_on: tuple[str, ...] = ()

    def render(self) -> str:
        lines = [f"Task {self.id}:", self.instruction.rstrip()]
        if self.paths:
            lines.append("Only these paths are yours: " + ", ".join(self.paths))
        if self.acceptance:
            lines.append("Acceptance gates (each must exit 0):")
            lines.extend(f"  - {cmd}" for cmd in self.acceptance)
        lines.append(TASK_Z_LINE)
        return "\n".join(lines)


@dataclass
class AgentResult:
    id: str
    role: str
    ok: bool
    requests: int = 0
    actions: list[str] = field(default_factory=list)
    error: str | None = None
    summary: str = ""
    # The agent's own final text, captured from MessageEvents. run_workflow needs
    # this to parse the architect's spec; without it a successful architect turn
    # would report only lane/model metadata and the spec would silently be empty.
    text: str = ""


# ----------------------------------------------------------- pre-flight checks
def check_prompt_stability(text: str, label: str = "prompt") -> None:
    """Refuse a prompt that can never produce a cache hit.

    Cheap to check, expensive to skip: one timestamp in a system prompt turns
    every later turn of that conversation into a full-price request.
    """
    for name, pattern in UNSTABLE_PATTERNS:
        hit = pattern.search(text or "")
        if hit:
            raise TeamError(
                f"{label} is not byte-stable across turns (found {name}: "
                f"{hit.group(0)[:32]!r}). A changing prefix invalidates the "
                "prompt cache for every later turn and this plan bills "
                "requests; rewrite it as fixed text.")


def compose_prompt(defn: Any, extra: str = "") -> str:
    """Role body plus optional shared contract, stability-checked."""
    body = (getattr(defn, "system_prompt", "") or "").strip()
    prompt = f"{body}\n\n{extra}".strip() if extra else body
    check_prompt_stability(prompt, f"role {getattr(defn, 'name', '?')} prompt")
    return prompt


def assert_disjoint(tasks: list[Task]) -> None:
    """Two tasks that may run together must not claim the same path."""
    seen: dict[str, str] = {}
    for task in tasks:
        for path in task.paths:
            owner = seen.get(path)
            if owner and owner != task.id:
                raise TeamError(
                    f"tasks {owner} and {task.id} both claim {path!r}; serialise "
                    "them or split the file. Running them together torn-writes it "
                    "and the damage only surfaces in someone else's test run.")
            seen[path] = task.id


# ------------------------------------------------------------------ the team
class Team:
    """Role files + router lanes, ready to run in one workspace."""

    def __init__(self, workspace: str | Path, *, router: Router | None = None,
                 roles_dir: str | Path | None = None,
                 max_requests: int | None = None):
        self.workspace = Path(workspace)
        if not self.workspace.is_dir():
            raise TeamError(f"workspace does not exist: {self.workspace}")
        self.router = router or Router.load()
        self.max_requests = max_requests
        self.results: list[AgentResult] = []
        self._requests = 0
        ensure_tools_registered()          # RC3: registry is empty until this
        try:
            defs = load_roles(roles_dir or ROLES_DIR)
        except RoleFileError as exc:
            raise TeamError(f"role files rejected: {exc}") from exc
        self.defs = {d.name: d for d in defs}
        missing = {"architect", "fixer", "reviewer"} - set(self.defs)
        if missing:
            raise TeamError(f"role files missing: {sorted(missing)}")
        self.ceiling = self._ceiling()

    def _ceiling(self) -> int:
        configured = int(self.router.budget.get("max_requests_per_team_run", 100))
        if self.max_requests is not None:
            if self.max_requests > configured:
                raise TeamError(f"--max-requests {self.max_requests} exceeds the "
                                f"configured ceiling of {configured}")
            return self.max_requests
        return configured

    def _charge(self, n: int = 1) -> None:
        if self._requests + n > self.ceiling:
            raise TeamError(
                f"team run would exceed its request ceiling ({self.ceiling}); "
                f"{self._requests} already spent. Stopping before the spend.")
        self._requests += n

    # -- construction ---------------------------------------------------
    def role_for(self, name: str) -> str:
        """Map a role-file name onto a router role name."""
        return name if name in self.router.roles else "fixer"

    def build_agent(self, name: str, *, extra_prompt: str = ""):
        """Construct a sub-agent and PROVE it can act, before any request.

        Returns ``(agent, llm, lane)``. Raises TeamError if the agent ends up
        with nothing but finish/think — the RC1 condition that cost the netflow
        run its quota.
        """
        if name not in self.defs:
            raise TeamError(f"no role file for {name!r}; have {sorted(self.defs)}")
        defn = self.defs[name]
        role = self.role_for(name)
        lane = self.router.lane_for(role)
        llm = self.router.build_llm(role)
        prompt = compose_prompt(defn, extra_prompt)
        patched = defn.model_copy(update={"system_prompt": prompt})
        try:
            agent = spawn_agent(patched, llm, work_dir=str(self.workspace),
                                require=tuple(defn.tools or ()))
        except RoleFileError as exc:
            raise TeamError(f"role {name}: {exc}") from exc
        return agent, llm, lane

    def conversation(self, name: str, *, extra_prompt: str = ""):
        """A Conversation bound to one role. Uses only factory-safe kwargs."""
        from openhands.sdk import Conversation

        agent, _llm, _lane = self.build_agent(name, extra_prompt=extra_prompt)
        role = self.role_for(name)
        return Conversation(agent=agent, workspace=str(self.workspace),
                            **self.router.public_conversation_kwargs(role))

    # -- planning (zero spend) ------------------------------------------
    def plan(self, brief_path: str | Path) -> dict[str, Any]:
        """Everything this team WOULD do, computed with zero API calls."""
        brief = Path(brief_path)
        out: dict[str, Any] = {
            "workspace": str(self.workspace),
            "brief": str(brief),
            "brief_readable": brief.is_file(),
            "brief_bytes": brief.stat().st_size if brief.is_file() else 0,
            "roles": {},
            "lanes": self.router.table(),
            "budget": {"ceiling": self.ceiling,
                       "configured_ceiling": self.router.budget.get(
                           "max_requests_per_team_run"),
                       "night_window": self.router.night_window_active()},
            "cache": {name: self.router.conversation_kwargs(self.role_for(name))
                      for name in sorted(self.defs)},
            "warnings": [],
        }
        if not brief.is_file():
            out["warnings"].append(f"brief not readable at {brief}")
        for name, defn in sorted(self.defs.items()):
            entry: dict[str, Any] = {"declared_tools": list(defn.tools),
                                     "max_iteration_per_run":
                                         defn.max_iteration_per_run,
                                     "runnable_tools": None, "lane": None,
                                     "error": None}
            try:
                agent, _llm, lane = self.build_agent(name)
                entry["runnable_tools"] = sorted(
                    {getattr(t, "name", str(t)) for t in agent.tools})
                entry["lane"] = f"{lane.name}:{lane.model}"
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                out["warnings"].append(f"role {name}: {entry['error']}")
            out["roles"][name] = entry
        return out

    # -- execution ------------------------------------------------------
    def run_task(self, task: Task) -> AgentResult:
        """Run one task through its role. This spends real requests."""
        role = task.role if task.role in self.defs else "fixer"
        events: list[str] = []
        texts: list[str] = []
        try:
            from openhands.sdk import Conversation

            agent, llm, lane = self.build_agent(role)

            def on_event(event) -> None:
                name = type(event).__name__
                if name == "ActionEvent":
                    events.append(type(getattr(event, "action", None)).__name__)
                elif name == "MessageEvent":
                    if getattr(event, "source", "") != "agent":
                        return
                    msg = getattr(event, "llm_message", None)
                    if msg is None:
                        return
                    for part in (msg.content or []):
                        chunk = getattr(part, "text", "") or ""
                        if chunk.strip():
                            texts.append(chunk)

            conv = Conversation(agent=agent, workspace=str(self.workspace),
                                callbacks=[on_event],
                                **self.router.public_conversation_kwargs(
                                    self.role_for(role)))
            self._charge()                       # one request charged up front
            conv.send_message(task.render())
            conv.run()
            calls = len(getattr(llm.metrics, "token_usages", []) or []) or 1
            self.router.record(self.role_for(role), calls)
            return AgentResult(id=task.id, role=role, ok=True, requests=calls,
                               actions=events, text="".join(texts),
                               summary=f"lane={lane.name} model={lane.model}")
        except TeamError:
            raise
        except Exception as exc:
            return AgentResult(id=task.id, role=role, ok=False, actions=events,
                               text="".join(texts),
                               error=f"{type(exc).__name__}: {str(exc)[:200]}")

    def map_agents(self, tasks: list[Task]) -> list[AgentResult]:
        """Run independent tasks concurrently; refuse if they are not disjoint.

        Concurrency is capped by the lane's ``concurrency_limit``. The plan bills
        requests against a rolling window, so N agents firing simultaneously is
        N simultaneous quota hits — parallelism here is a latency win, not free
        throughput.
        """
        if not tasks:
            return []
        assert_disjoint(tasks)
        lanes = [self.router.lane_for(self.role_for(t.role
                     if t.role in self.defs else "fixer")) for t in tasks]
        workers = min(len(tasks), max(l.concurrency_limit for l in lanes))
        if workers <= 1:
            results = [self.run_task(t) for t in tasks]
        else:
            with cf.ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(self.run_task, tasks))
        self.results.extend(results)
        return results

    def run_workflow(self, brief_path: str | Path, *,
                     spec_parser: Callable[[str], list[Task]] | None = None,
                     ) -> dict[str, Any]:
        """architect spec -> parallel fixers -> reviewer, with accounting.

        ``spec_parser`` turns the architect's free text into Tasks. It defaults
        to :func:`parse_tasks`, which reads a fenced JSON block; pass something
        stricter for production, because a loosely parsed spec is how two fixers
        end up owning the same file.
        """
        report: dict[str, Any] = {"steps": [], "requests_charged": 0}
        spec_result = self.run_task(Task(
            id="spec", role="architect",
            instruction=("Read the brief at " + str(brief_path) + ". Produce a "
                         "numbered task spec. For each task give: id, role "
                         "(fixer/tester/reviewer), one instruction, the exact "
                         "file paths it may touch, and acceptance commands that "
                         "must exit 0. Emit the tasks as a single fenced JSON "
                         "array at the end of your reply. Do not edit any file."),
            paths=(), acceptance=()))
        report["steps"].append(self._as_dict(spec_result))
        if not spec_result.ok:
            report["aborted"] = "architect failed: " + str(spec_result.error)
            report["requests_charged"] = self._requests
            return report

        tasks = (spec_parser or parse_tasks)(spec_result.text)
        report["tasks_parsed"] = len(tasks)
        if not tasks:
            # Loud, not silent: an architect that returns prose with no task
            # block is a failed step, and skipping fixers quietly would let the
            # run report success having built nothing.
            report["warnings"] = (report.get("warnings") or []) + [
                "architect returned no parseable task block; fixers were skipped"]
        else:
            report["steps"].extend(self._as_dict(r)
                                   for r in self.map_agents(tasks))

        review = self.run_task(Task(
            id="review", role="reviewer",
            instruction=("Verify each acceptance gate in this workspace by "
                         "running it. Report PASS or FAIL per task with the "
                         "command output that justifies it. Write the verdict "
                         "to DONE.md."),
            paths=("DONE.md",), acceptance=("python3 lib/secret_scan.py",)))
        report["steps"].append(self._as_dict(review))
        report["requests_charged"] = self._requests
        return report

    @staticmethod
    def _as_dict(result: AgentResult) -> dict[str, Any]:
        # text is deliberately NOT included: an agent reply can quote a file
        # that quotes a credential, and reports get pasted into chat. Capture
        # length instead; full text belongs in a redacted runs/ file.
        return {"id": result.id, "role": result.role, "ok": result.ok,
                "requests": result.requests, "actions": result.actions,
                "error": result.error, "summary": result.summary,
                "text_len": len(result.text or "")}


# ------------------------------------------------------------- spec parsing
#: Acceptance commands are REPORTED, never executed by this module. They come
#: from model output, and shelling out to model-authored strings is how a
#: "review" step becomes an arbitrary-code-execution step. A human or the
#: reviewer agent runs them; the orchestrator only records them.
def parse_tasks(text: str) -> list[Task]:
    """Extract a JSON task array from an architect's reply.

    Tolerates prose around the block and a top-level {"tasks": [...]} wrapper,
    because models do both. Anything unparseable returns [] rather than a
    half-read spec — a silently wrong task list is worse than no task list.
    """
    if not text:
        return []
    candidates = re.findall(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", text, re.S)
    candidates += re.findall(r"(\[\s*\{.*\}\s*\])", text, re.S)
    for blob in candidates:
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            data = data.get("tasks") or []
        if not isinstance(data, list):
            continue
        tasks: list[Task] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict) or "instruction" not in item:
                continue
            tasks.append(Task(
                id=str(item.get("id") or f"T{i + 1}"),
                role=str(item.get("role") or "fixer"),
                instruction=str(item["instruction"]),
                paths=tuple(str(p) for p in (item.get("paths") or ())),
                acceptance=tuple(str(a) for a in (item.get("acceptance") or ())),
                depends_on=tuple(str(d) for d in (item.get("depends_on") or ()))))
        if tasks:
            return tasks
    return []


# ----------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="run_team.py",
        description="Run the architect -> parallel fixers -> reviewer workflow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples
  # validate everything with ZERO API calls. Do this first, every time.
  python3 team/run_team.py --plan --brief ~/.hermes/qwen-multiagent-brief.md

  # the real thing, with a hard request ceiling below the configured one
  python3 team/run_team.py --brief PATH --workspace ../scratch-copy --max-requests 60

  # print the router's lane table and budget
  python3 team/run_team.py --lanes

why --plan exists
  RC1 (repro/FINDINGS.md) cost an entire team run because a role file parsed to
  tools=[] and nothing raised. --plan runs the same assertions -- frontmatter
  parsed, tools registered, every role actually runnable, prompts cache-stable --
  for free. If --plan fails, the run would have failed too, expensively.
""")
    ap.add_argument("--brief", default=os.environ.get("TEAM_BRIEF", ""),
                    help="path to the brief the architect reads")
    ap.add_argument("--workspace", default=str(REPO),
                    help="repo the team works in (default: this repo)")
    ap.add_argument("--plan", action="store_true",
                    help="validate and print the plan; spends 0 requests")
    ap.add_argument("--lanes", action="store_true",
                    help="print the router lane table; spends 0 requests")
    ap.add_argument("--max-requests", type=int, default=None,
                    help="hard ceiling for this run (must be <= router ceiling)")
    ap.add_argument("--config", default=None, help="alternate router.yaml")
    ap.add_argument("--roles-dir", default=None, help="alternate roles directory")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    router = Router.load(args.config) if args.config else Router.load()

    if args.lanes:
        for row in router.table():
            print(f"{row['role']:13s} {row['lane']:18s} {row['model']:28s} "
                  f"trusted={row['trusted']} parallel={row['parallel']}")
        print(json.dumps(router.budget_report(), indent=2, default=str))
        return 0

    try:
        team = Team(args.workspace, router=router, roles_dir=args.roles_dir,
                    max_requests=args.max_requests)
    except TeamError as exc:
        print(f"REFUSED BEFORE SPEND: {exc}", file=sys.stderr)
        return 2

    if args.plan:
        if not args.brief:
            print("REFUSED: --plan needs --brief PATH", file=sys.stderr)
            return 2
        plan = team.plan(args.brief)
        if args.json:
            print(json.dumps(plan, indent=2, default=str))
        else:
            print(f"workspace : {plan['workspace']}")
            print(f"brief     : {plan['brief']} "
                  f"({plan['brief_bytes']} bytes, readable={plan['brief_readable']})")
            print(f"ceiling   : {plan['budget']['ceiling']} requests; "
                  f"night window active={plan['budget']['night_window'].get('active')}")
            for name, info in sorted(plan["roles"].items()):
                status = "OK  " if not info["error"] else "FAIL"
                print(f"  [{status}] {name:10s} lane={info['lane']} "
                      f"runnable={info['runnable_tools']} "
                      f"max_iter={info['max_iteration_per_run']}")
                if info["error"]:
                    print(f"         {info['error']}")
            print(f"cache keys: " + json.dumps(
                {k: v.get("prompt_cache_key") for k, v in plan["cache"].items()}))
            if plan["warnings"]:
                print("\nWARNINGS:")
                for w in plan["warnings"]:
                    print("  - " + w)
                return 1
            print("\nPLAN OK: every role is runnable; 0 requests spent")
        return 0

    if not args.brief:
        print("REFUSED: --brief PATH is required (or use --plan/--lanes)",
              file=sys.stderr)
        return 2
    if not Path(args.brief).is_file():
        print(f"REFUSED: brief not found at {args.brief}", file=sys.stderr)
        return 2

    report = team.run_workflow(args.brief)
    print(json.dumps(report, indent=2, default=str))
    failed = [s for s in report["steps"] if not s["ok"]]
    print(f"\nsteps={len(report['steps'])} failed={len(failed)} "
          f"requests_charged={report['requests_charged']} ceiling={team.ceiling}")
    if report.get("warnings"):
        for w in report["warnings"]:
            print("WARNING: " + w)
    return 1 if failed or report.get("aborted") else 0


if __name__ == "__main__":
    raise SystemExit(main())
