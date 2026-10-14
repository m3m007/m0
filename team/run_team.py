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
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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

#: wall clock minus monotonic clock, sampled once at import. Adding it turns
#: ``time.monotonic()`` into a never-moving-backwards timestamp that is still
#: comparable with ``stat().st_mtime`` (see Team._spec_start_stamp).
_MONO_TO_WALL = time.time() - time.monotonic()

#: The Task-Z self-wake line, appended verbatim to every task.
#:
#: The status FILE IS PER-TASK, not shared. A single DONE.md is a collision I
#: actually hit in the first live smoke run: two parallel fixers both tried to
#: write it, one found it occupied and had to append around the other. With
#: per-task files, "is this task done?" is answerable without two agents racing
#: on one inode, and a stale DONE.md from an earlier run cannot masquerade as
#: this run's status.
#:
#: The sentence itself is identical for every task except the filename, so the
#: shared prefix stays cache-stable (rule 1).
TASK_Z_TEMPLATE = (
    "TASK-Z: if you get blocked, cannot proceed, or finish, still write your "
    "status to {status_file} in this workspace and call finish anyway - the "
    "next agent wakes on that file, not on you.")
DEFAULT_STATUS_FILE = "DONE.md"


def task_z_line(status_file: str = DEFAULT_STATUS_FILE) -> str:
    """The self-wake instruction, naming the file this task alone owns."""
    return TASK_Z_TEMPLATE.format(status_file=status_file)


#: Back-compatible name for the generic line (used in docs and older call sites).
TASK_Z_LINE = task_z_line()


def status_file_for(task_id: str) -> str:
    """The per-task status filename an agent must write."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id or "task")
    return f"DONE-{safe}.md"


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
        lines.append(task_z_line(self.status_file))
        return "\n".join(lines)

    @property
    def status_file(self) -> str:
        """The one status file this task owns. Per-task by construction, so
        parallel agents never write to the same file."""
        return status_file_for(self.id)


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
    """Two tasks that may run together must not claim the same path.

    R5-D2: containment, not string equality. `tests/` and `tests/test_x.py` DO
    overlap -- an exact-string compare passed that pair, and the resulting
    parallel torn-write looked like a flaky test suite. One shared helper
    (:func:`covers_path`) now answers overlap here and protected-path tampering
    in the grading stage, so the two can never disagree about what "same file"
    means.
    """
    seen: dict[str, str] = {}
    claims: list[tuple[str, str]] = []
    for task in tasks:
        for path in task.paths:
            claims.append((task.id, path))
            owner = seen.get(path)
            if owner and owner != task.id:
                raise TeamError(
                    f"tasks {owner} and {task.id} both claim {path!r}; serialise "
                    "them or split the file. Running them together torn-writes it "
                    "and the damage only surfaces in someone else's test run.")
            seen[path] = task.id
    for (id_a, a), (id_b, b) in itertools.combinations(claims, 2):
        if id_a == id_b:
            continue                      # one task naming its own path twice
        if covers_path(a, b) or covers_path(b, a):
            raise TeamError(
                f"tasks {id_a} and {id_b} both claim {a!r} and {b!r}, which are "
                "the same file or nested inside each other; serialise them or "
                "split the file. Running them together torn-writes it and the "
                "damage only surfaces in someone else's test run.")


def norm_path(path: str | Path, root: str | Path | None = None) -> str:
    """Absolute, normalised, symlink-resolved path, `./` and trailing `/` gone.

    Every path comparison in this engine goes through here. Relative paths are
    resolved against `root` (or the cwd), which is why a git-reported path
    (always repo-root relative) and a task-declared path (workspace relative)
    become comparable at all.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    if not os.path.isabs(text):
        base = str(root) if root is not None else str(Path.cwd())
        text = os.path.join(base, text)
    return os.path.realpath(os.path.normpath(text))


def covers_path(outer: str | Path, inner: str | Path, *,
                root: str | Path | None = None) -> bool:
    """True when `outer` IS `inner` or a parent DIRECTORY of it.

    Containment, not prefix strings: `tests/` covers `tests/test_x.py`, `pkg`
    does not cover `pkgfoo.py`. A declared directory covers everything under it
    because an agent handed a directory owns its children.
    """
    a, b = norm_path(outer, root), norm_path(inner, root)
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        return os.path.commonpath([a, b]) == a
    except ValueError:
        return False          # different drives/roots: never a containment



# ------------------------------------------------------- R5: post-run grading
# The discipline teamkit built in R1/R2, living in the engine so BOTH entry
# points grade the same way. teamkit re-exports these (single implementation of
# each) and keeps only the signatures its records API has always published.
GRADE_TIMEOUT = 900
#: The five outcome classes (R2). "The system failed" and "the system did
#: nothing" are different diseases: a failed gate needs a better brief, a
#: zero-work stall needs a lane fix.
OUTCOMES = ("success", "partial_work", "abstained", "refused", "tie_kept")
#: CLI exit map (documented in README): 0 pass-family, 1 failed gate, 2 tie (the
#: work is not proven better than the incumbent) AND every refuse-before-spend
#: path the CLI already had, 3 graded-run abort. Refusing before spend has always
#: exited 2 here, and that behaviour is deliberately unchanged.
EXIT_PASS = 0
EXIT_GATE_FAIL = 1
EXIT_TIE = 2
EXIT_REFUSED = 2
EXIT_ABORTED = 3


def grade_exit_code(status: str | None, aborted: bool = False,
                    failed_steps: int = 0) -> int:
    """The CLI exit map for a graded run (documented in README).

    0 only for the pass-family; GATE_FAIL is 1; a TIE is 2, because the work is
    kept but not proven better; a graded run that aborted mid-flight is 3, which
    is different damage from a failing gate. Refused-before-spend keeps the CLI's
    pre-existing 2 at its own call sites.
    """
    if aborted:
        return EXIT_ABORTED
    if status in ("GATE_PASS", "GATE_PASS_NEW"):
        return EXIT_PASS if not failed_steps else EXIT_GATE_FAIL
    if status == "GATE_PASS_TIE":
        return EXIT_TIE
    if status == "RUN_DONE_NO_GATE":
        return EXIT_PASS if not failed_steps else EXIT_GATE_FAIL
    return EXIT_GATE_FAIL


def git_root(workspace: str | Path) -> str | None:
    """The work tree hosting `workspace` (walks up), or None when not a repo."""
    try:
        proc = subprocess.run(["git", "-C", str(workspace), "rev-parse",
                               "--show-toplevel"], capture_output=True,
                              text=True, timeout=60)
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    return out if proc.returncode == 0 and out else None


def _git_out(args: list[str], cwd: str | Path | None = None) -> str:
    try:
        proc = subprocess.run(args, cwd=str(cwd) if cwd else None,
                              capture_output=True, text=True, timeout=120)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def capture_baseline_ref(workspace: str | Path) -> str | None:
    """Pristine baseline commit, captured BEFORE the team touches anything.

    Dirty work tree -> `git stash create` (the object is created, never applied:
    the user's workdir is not mutated). Clean -> HEAD. Untracked-only dirt
    yields HEAD, which is the documented R1 semantics.
    """
    root = git_root(workspace)
    if not root:
        return None
    if _git_out(["git", "-C", root, "status", "--porcelain"], root):
        sc = _git_out(["git", "-c", "user.email=teamkit@local",
                       "-c", "user.name=teamkit", "stash", "create"], root)
        if sc:
            return sc
    return _git_out(["git", "-C", root, "rev-parse", "HEAD"], root) or None


def materialize_baseline(baseline_ref: str, workspace: str | Path):
    """Pristine copy of `baseline_ref` in a temp dir, (dest, cleanup) or (None, None).

    `git worktree add --detach` first; `git archive | tar -x` when a tree refuses
    worktrees. Cleanup removes the tree AND prunes the internal worktree
    registration, so the user's repo keeps exactly one worktree.
    """
    root = git_root(workspace)
    if not root:
        return None, None
    dest = tempfile.mkdtemp(prefix="teamkit-baseline-",
                            dir=os.environ.get("TEAMKIT_BASE_TMP") or None)
    made_worktree = False

    def cleanup():
        if made_worktree:
            subprocess.run(["git", "-C", root, "worktree", "remove", "--force",
                            dest], capture_output=True, text=True)
        else:
            shutil.rmtree(dest, ignore_errors=True)
        subprocess.run(["git", "-C", root, "worktree", "prune"],
                       capture_output=True, text=True)

    try:
        proc = subprocess.run(["git", "-C", root, "worktree", "add", "--detach",
                               dest, baseline_ref], capture_output=True, text=True)
        if proc.returncode == 0:
            made_worktree = True
        else:
            shutil.rmtree(dest, ignore_errors=True)
            dest = tempfile.mkdtemp(prefix="teamkit-baseline-",
                                    dir=os.environ.get("TEAMKIT_BASE_TMP") or None)
            arc = subprocess.run(["git", "-C", root, "archive", "--format=tar",
                                  baseline_ref], capture_output=True)
            if arc.returncode != 0:
                return None, None
            tar = subprocess.run(["tar", "-x", "-C", dest], input=arc.stdout)
            if tar.returncode != 0:
                return None, None
        rel = os.path.relpath(os.path.abspath(str(workspace)), root)
        return (os.path.join(dest, rel) if rel and rel != "." else dest), cleanup
    except Exception:
        cleanup()
        return None, None


def run_gate(cmd: str, cwd: str | Path) -> dict[str, Any]:
    """One gate command, timeout 900, cwd=workspace -> {ok, rc, ran, tail, error}."""
    try:
        proc = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True,
                              text=True, timeout=GRADE_TIMEOUT)
        return {"ok": proc.returncode == 0, "rc": proc.returncode, "ran": True,
                "tail": ((proc.stdout or "")[-500:] + (proc.stderr or "")[-300:]),
                "error": None}
    except Exception as exc:
        return {"ok": False, "rc": None, "ran": False, "tail": str(exc)[-500:],
                "error": f"{type(exc).__name__}: {str(exc)[-300:]}"}


def capture_workspace_state(workspace: str | Path) -> dict[str, Any]:
    """READ-ONLY fingerprint of a repo: HEAD, tracked diff, dirt, digests.

    R5-D2 work evidence is computed from two of these, taken BEFORE the gate so
    a gate that runs a test suite cannot credit the team with __pycache__ dirt.
    """
    root = git_root(workspace)
    if not root:
        return {"root": None, "ws": str(workspace), "head": "", "tracked": "",
                "porcelain": "", "tracked_sha": "", "porcelain_sha": ""}
    porcelain = _git_out(["git", "-C", root, "status", "--porcelain", "-uall"],
                         root)
    tracked = _git_out(["git", "-C", root, "diff", "--no-color", "HEAD"], root)
    return {"root": root, "ws": str(workspace),
            "head": _git_out(["git", "-C", root, "rev-parse", "HEAD"], root),
            "tracked": tracked, "tracked_sha": _sha(tracked),
            "porcelain": porcelain, "porcelain_sha": _sha(porcelain)}


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def work_evidence(base: dict | None, after: dict | None) -> dict[str, Any]:
    """What the run produced between two fingerprints (R2 semantics)."""
    base = base or {}
    after = after or {}
    b_head, a_head = base.get("head") or "", after.get("head") or ""
    commits: int | None = None
    if b_head and a_head:
        if b_head == a_head:
            commits = 0
        else:
            out = _git_out(["git", "-C", str(after.get("root")
                                                  or base.get("root") or "."),
                            "rev-list", "--count", f"{b_head}..{a_head}"])
            try:
                commits = int(out)
            except ValueError:
                commits = None
    known = set((base.get("porcelain") or "").splitlines())
    return {"commits": commits,
            "tracked_changed": (base.get("tracked_sha") or "") !=
                               (after.get("tracked_sha") or ""),
            "dirt_changed": (base.get("porcelain_sha") or "") !=
                            (after.get("porcelain_sha") or ""),
            "new_dirt": [ln for ln in (after.get("porcelain") or "").splitlines()
                         if ln and ln not in known],
            "measurable": commits is not None}


def has_work(ev: dict | None) -> bool:
    """Evidence says the run left something behind. Unmeasurable is never
    reported as 'did nothing' — we cannot prove the absence of work."""
    if not ev or not ev.get("measurable"):
        return True
    return bool(ev.get("commits")) or bool(ev.get("tracked_changed")) or \
        bool(ev.get("dirt_changed"))


def _porcelain_paths(text: str) -> set[str]:
    """Every path a `git status --porcelain` block mentions, first field wins.

    Parsed rather than compared line-wise: porcelain's status column carries
    leading spaces, and the whole block read by _git_out() is stripped, so a
    line's exact text depends on whether it is first in the output. The path is
    the only stable part. Renames report 'old -> new'; the new name is the one
    that changed.
    """
    paths = set()
    for ln in (text or "").splitlines():
        ln = ln.rstrip()
        if not ln:
            continue
        body = ln[2:] if len(ln) > 2 else ""
        if " -> " in body:
            body = body.split(" -> ", 1)[1]
        path = body.strip().strip('"')
        if path:
            paths.add(path)
    return paths


def changed_paths(baseline: dict, after: dict, workspace: str | Path) -> list[str]:
    """Every path this run could be blamed for: committed diff + working dirt.

    Committed: `git diff --name-only <HEAD-at-start>..<HEAD-now>`. Uncommitted:
    `git status --porcelain -uall` minus the paths already dirty at run start
    (the owner's own edits are not the team's). Both are reported repo-root
    relative and resolved to absolute paths here, because the comparison that
    follows is containment, and containment needs one shared coordinate space.
    """
    root = after.get("root") or git_root(workspace)
    if not root:
        return []
    paths: list[str] = []
    base_head, after_head = baseline.get("head") or "", after.get("head") or ""
    if base_head and after_head and base_head != after_head:
        out = _git_out(["git", "-C", root, "diff", "--name-only",
                        f"{base_head}..{after_head}"], root)
        paths += [ln for ln in out.splitlines() if ln.strip()]
    new_dirt = _porcelain_paths(after.get("porcelain")) - \
        _porcelain_paths(baseline.get("porcelain"))
    paths += sorted(new_dirt)
    resolved, seen = [], set()
    for p in paths:
        ap = norm_path(p, root)
        if ap and ap not in seen:
            seen.add(ap)
            resolved.append(ap)
    return resolved


def protect_violations(protect: list[str] | tuple[str, ...],
                       tasks: list[Task], changed: list[str],
                       root: str | Path,
                       declared_paths: list[str] | None = None) -> list[str]:
    """Changed paths that match a protect entry and no task's declared paths.

    Containment both ways (D2): a protected directory protects its children, and
    a task that declared that same directory is allowed to change inside it.
    `declared_paths` adds declarations that come from outside the spec, namely
    the runtime's own (a facade agent legitimately owns the workspace it runs
    in). Returns '<protect entry>:<changed file>' for every violation, so the
    status reason can name both sides of the breach.
    """
    bad: list[str] = []
    claims = [claim for task in (tasks or []) for claim in task.paths]
    claims += list(declared_paths or ())
    for entry in protect or ():
        if not str(entry).strip():
            continue
        for path in changed:
            if not covers_path(entry, path, root=root):
                continue
            allowed = any(covers_path(claim, path, root=root) for claim in claims)
            if not allowed:
                bad.append(f"{entry}:{path}")
    return bad


def evaluate_grade(workspace: str | Path, contract: dict | None, *,
                   tasks: list[Task] | None = None,
                   baseline: dict | None = None,
                   after: dict | None = None,
                   baseline_ref: str | None = None,
                   declared_paths: list[str] | None = None,
                   ) -> dict[str, Any]:
    """The deterministic grading stage: integrity check, gate, pair, outcome.

    Runs AFTER the workflow, never inside an agent prompt. Order matters: the
    protected-path integrity check happens BEFORE the gate and can fail the run
    regardless of the gate's exit code, because an agent that rewrote the test
    it was supposed to satisfy has not fixed anything.
    """
    contract = contract or {}
    gate_cmd = contract.get("gate")
    inc_cmd = contract.get("incumbent_gate")
    protect = list(contract.get("protect") or ())
    root = git_root(workspace) or str(Path(workspace).resolve())
    out: dict[str, Any] = {"status": None, "gate": gate_cmd, "gate_tail": "",
                           "pair": None, "outcome": None,
                           "work_evidence": None, "protect": protect or None,
                           "violations": []}
    ev = work_evidence(baseline, after) if (baseline is not None and
                                            after is not None) else None
    out["work_evidence"] = ev

    violations: list[str] = []
    if protect and baseline is not None and after is not None:
        changed = changed_paths(baseline, after, workspace)
        violations = protect_violations(protect, tasks, changed, root,
                                        declared_paths=declared_paths)
    out["violations"] = violations

    if not gate_cmd:
        # A tamper check still outranks "there was no gate to run".
        out["status"] = ("GATE_FAIL" if violations else "RUN_DONE_NO_GATE")
        if violations:
            out["reason"] = "protected-path-tampered:" + violations[0]
        out["outcome"] = classify_outcome(out["status"], ev)
        return out

    if violations:
        out["status"] = "GATE_FAIL"
        out["reason"] = "protected-path-tampered:" + violations[0]
        out["outcome"] = classify_outcome(out["status"], ev)
        return out

    r_new = run_gate(gate_cmd, workspace)
    out["gate_tail"] = r_new["tail"]
    if not inc_cmd:
        # Legacy single-gate path: GATE_PASS is emitted only here.
        out["status"] = "GATE_PASS" if r_new["ok"] else "GATE_FAIL"
        out["outcome"] = classify_outcome(out["status"], ev)
        return out

    pair = None
    baseline_ref = baseline_ref or capture_baseline_ref(workspace)
    bdir, cleanup = None, None
    r_base = {"ok": False, "rc": None, "tail": "", "ran": False,
              "error": "no baseline ref: workspace is not a git work tree"}
    try:
        if baseline_ref:
            bdir, cleanup = materialize_baseline(baseline_ref, workspace)
            if bdir:
                r_base = run_gate(inc_cmd, bdir)
            else:
                r_base = {"ok": False, "rc": None, "tail": "", "ran": False,
                          "error": "baseline materialization failed"}
    except Exception as exc:
        r_base = {"ok": False, "rc": None, "tail": str(exc)[-500:], "ran": False,
                  "error": f"{type(exc).__name__}: {str(exc)[-300:]}"}
    finally:
        if cleanup:
            try:
                cleanup()
            except Exception:
                pass
    if not r_new["ok"]:
        out["status"] = "GATE_FAIL"                    # incumbent retained
    elif r_base["ok"]:
        out["status"] = "GATE_PASS_TIE"                # matches the baseline
    elif r_base["ran"]:
        out["status"] = "GATE_PASS_NEW"                # proven improvement
    else:
        # A pair check that could not run proves nothing about the incumbent,
        # so no improvement is claimed.
        out["status"] = "GATE_PASS_TIE"
    g_new = {"ok": r_new["ok"], "rc": r_new["rc"], "cmd": gate_cmd,
             "error": r_new["error"]}
    g_base = {"ok": r_base["ok"], "rc": r_base["rc"], "cmd": inc_cmd,
              "error": r_base["error"], "ran": bool(r_base.get("ran", True))}
    head_ref = _git_out(["git", "-C", root, "rev-parse", "HEAD"], root)
    out["pair"] = {"g_new": g_new, "g_base": g_base,
                   "baseline_ref": baseline_ref or None}
    out["retained"] = {"accepted": bool(r_new["ok"]),
                       "baseline_ref": baseline_ref or None,
                       "head_ref": head_ref or None,
                       "g_new": g_new, "g_base": g_base}
    out["gate_tail"] = (r_new["tail"] + "\n" + r_base["tail"])[-800:]
    out["outcome"] = classify_outcome(out["status"], ev)
    return out


def classify_outcome(status: str | None, ev: dict | None) -> str:
    """R2 split. With a work_evidence dict the abstained/partial_work question
    is answered by git; without one, only the status-derived classes are safe.
    """
    if status in ("GATE_PASS", "GATE_PASS_NEW"):
        return "success"
    if status == "GATE_PASS_TIE":
        return "tie_kept"
    if status in ("BUDGET_REFUSED", "DRY_PLAN"):
        return "refused"
    if ev is None:
        return "partial_work"
    return "partial_work" if has_work(ev) else "abstained"


# --------------------------------------------------- R5-D1: run records in the
# engine. One runs/ tree for BOTH entry points: teamkit re-exports these names
# rather than keeping a second implementation, so a record written by the CLI
# and a record written by a netflow caller are the same object shape.
RUNS_ROOT_DEFAULT = REPO / "runs"
GRADE_STAGES = ("spec", "team_run", "gate", "lessons")


class RunRecordError(RuntimeError):
    """The record machinery failed in a way the caller must see."""


def run_slug(workspace: str | Path) -> str:
    """Slug = first 24 safe chars of basename(workspace)."""
    name = os.path.basename(str(workspace).rstrip("/"))
    return re.sub(r"[^A-Za-z0-9._-]", "", name)[:24]


def unique_run_dir(runs_root: str | Path, slug: str) -> str:
    """runs/<utc ts>-<slug>; a second run in the same second gets -2, -3 ..."""
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = os.path.join(str(runs_root), f"{ts}-{slug}")
    if not os.path.exists(base):
        return base
    n = 2
    while os.path.exists(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def runs_root(explicit: str | Path | None = None) -> str:
    return str(explicit or os.environ.get("TEAMKIT_RUNS_DIR")
               or RUNS_ROOT_DEFAULT)


def write_run_record(run_dir: str | Path, result: dict[str, Any], *,
                    config: dict[str, Any] | None = None,
                    state: dict[str, Any] | None = None,
                    retained: dict[str, Any] | None = None) -> str:
    """Persist summary.json (+ config/state/retained when given). Returns path."""
    root = Path(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in result.items()}
    payload["run_dir"] = str(root)
    for name, obj in (("config.json", config), ("state.json", state),
                      ("summary.json", payload),
                      ("retained.json", retained)):
        if obj is None:
            continue
        tmp = root / (name + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str),
                       encoding="utf-8")
        os.replace(tmp, root / name)
    return str(root)


# ------------------------------------------------------------------ the team
class TeamRuntime:
    """The R5-D3 injection seam: how a Team loads roles, builds LLMs, runs work.

    The default keeps the engine exactly as it was proven live: strict role-file
    loading (RC1), router-resolved lane kwargs, one Conversation per role.
    teamkit's facade passes a subclass that runs its agent force-refresh before
    the first request, resolves the credential through `lib.keys` matched to the
    lane's base URL, and hands a builder-mode task its own conversation, so ONE
    engine and ONE grading path serve both entry points while each caller keeps
    the rules it was born with.
    """

    def __init__(self) -> None:
        #: Raise out of run_task instead of turning a crash into a failed result.
        #: The facade sets it: teamkit's callers expect conv.run() blowups to
        #: propagate, and the run record must say `ERROR`.
        self.propagate_task_errors = False
        #: The engine's RC1 promise: the three workflow roles must exist. A
        #: runtime that registers its own definitions (teamkit) turns this off
        #: and does the checking its own way.
        self.strict_roles = True
        #: R5-D3 builder mode: a single task to run in place of the workflow.
        self.direct_task: Task | None = None
        #: Extra text appended to the message a task is sent (contract + memory).
        self.message_suffix = ""
        #: Wholesale replacement for the task rendering (facade runs send the
        #: brief + contract + memory block, not a rendered Task).
        self.message_override: str | None = None
        #: Keys merged into the router-resolved LLM kwargs (facade model
        #: override and its credential resolution).
        self.llm_overrides: dict[str, Any] = {}
        #: Paths the RUNTIME itself owns in the workspace, so a facade agent
        #: that legitimately writes its status file is not failed for it.
        self.declared_paths: list[str] = []
        #: Fired by a runtime that owns the conversation, exactly where its
        #: agent touches the workspace (teamkit's test seam).
        self.on_run: Callable[[str], None] | None = None
        #: Recorded as `api_key_source` in config.json: how this runtime keys.
        self.api_key_source = "lane"
        #: Conversations this runtime built, newest last (orchestrator_final).
        self.conversations: list[Any] = []

    def load_defs(self, roles_dir: str | Path) -> list[Any]:
        """Role definitions, with the RC3 registry warm-up the engine needs."""
        ensure_tools_registered()
        try:
            return load_roles(roles_dir)
        except RoleFileError as exc:
            raise TeamError(f"role files rejected: {exc}") from exc

    def preflight(self, team: "Team", iter_cap: int) -> dict[str, Any] | None:
        """Run before the first request can be spent. Default: no-op.

        Returns the `agents` block for config.json, or None.
        """
        return None

    def llm_kwargs(self, team: "Team", role: str) -> dict[str, Any]:
        """The LLM kwargs for one role. Default: exactly what the engine did."""
        return team.router.llm_kwargs(role)

    def build_llm(self, team: "Team", kwargs: dict[str, Any]):
        from openhands.sdk import LLM

        return LLM(**kwargs)

    def prepare_task(self, team: "Team", task: "Task"):
        """(conversation, llm, summary) when the runtime owns the whole run.

        Default None: the engine builds the conversation from the role file.
        """
        return None

    def record_api_key(self) -> str:
        """The credential resolution this runtime uses, for the record.

        'lib.keys' means the key was matched to the lane's token-plan base URL
        instead of being the first `api_key:` line in the owner's config.
        """
        return self.api_key_source


class Team:
    """Role files + router lanes, ready to run in one workspace.

    ``dry_run=True`` walks the entire workflow -- architect prompt, spec parsing,
    overlap checks, reviewer step -- while sending nothing. It exists because the
    cost of discovering a wiring mistake at runtime is measured in requests, and
    on this plan requests are the thing we are trying to save.
    """

    #: What a dry run pretends the architect wrote, so the happy path is tested.
    DRY_SPEC = json.dumps([
        {"id": "T1", "role": "fixer", "instruction": "Implement the first fix.",
         "paths": ["dry_a.py"], "acceptance": ["python3 -m py_compile dry_a.py"]},
        {"id": "T2", "role": "fixer", "instruction": "Implement the second fix.",
         "paths": ["dry_b.py"], "acceptance": ["python3 -m py_compile dry_b.py"]},
    ], indent=2)

    def __init__(self, workspace: str | Path, *, router: Router | None = None,
                 roles_dir: str | Path | None = None,
                 max_requests: int | None = None, dry_run: bool = False,
                 contract: dict[str, Any] | None = None,
                 grade: bool | None = None,
                 runtime: TeamRuntime | None = None,
                 runs_root: str | Path | None = None,
                 record_name: str | None = None,
                 record: bool | None = None):
        self.workspace = Path(workspace)
        if not self.workspace.is_dir():
            raise TeamError(f"workspace does not exist: {self.workspace}")
        self.router = router or Router.load()
        self.max_requests = max_requests
        self.dry_run = dry_run
        #: R5-D3: how this team builds LLMs / prepares agents (see TeamRuntime).
        self.runtime = runtime or TeamRuntime()
        self.runs_root = runs_root
        #: Name for the record payload (the engine calls it a report).
        self.record_name = record_name
        #: False => the CALLER writes the record (teamkit owns the slug lock and
        #: the config.json shape); None/True => this engine writes one per run.
        self._record = record
        self.agent_summary: dict[str, Any] | None = None
        #: R5-D1/D2: the grading contract ({gate, incumbent_gate, protect}).
        #: Grading defaults ON as soon as a contract is handed over — an EMPTY
        #: contract counts, because "the run finished and there was no gate to
        #: run" is the RUN_DONE_NO_GATE verdict, not an ungraded run. Without a
        #: contract at all the report stays byte-compatible with pre-R5.
        self.contract = dict(contract) if contract is not None else None
        self.grade = (self.contract is not None) if grade is None else bool(grade)
        self.baseline: dict[str, Any] | None = None
        self.baseline_ref: str | None = None
        self.after_team: dict[str, Any] | None = None
        self.tasks: list[Task] = []
        self.results: list[AgentResult] = []
        self._requests = 0
        self._spec_done = False
        self._seeded_spec = False
        # R5-D3: role loading goes through the runtime. The engine's own runtime
        # warms the tool registry (RC3) and applies strict RC1 loading exactly as
        # before; the facade's runtime skips that because it registers its own
        # definitions and must not reject a builder run over a hollow md file.
        defs = self.runtime.load_defs(roles_dir or ROLES_DIR)
        self.defs = {d.name: d for d in defs}
        missing = {"architect", "fixer", "reviewer"} - set(self.defs)
        if missing and not self._roles_optional():
            raise TeamError(f"role files missing: {sorted(missing)}")
        self.ceiling = self._ceiling()

    def _roles_optional(self) -> bool:
        """Builder mode (a direct task, no delegation) needs no role files, and
        neither does a runtime that registers its own definitions (teamkit)."""
        return (self.runtime.direct_task is not None
                or not getattr(self.runtime, "strict_roles", True))

    def _ceiling(self) -> int:
        configured = int(self.router.budget.get("max_requests_per_team_run", 100))
        if self.max_requests is not None:
            if self.max_requests > configured:
                raise TeamError(f"--max-requests {self.max_requests} exceeds the "
                                f"configured ceiling of {configured}")
            return self.max_requests
        return configured

    def clear_stale_status_files(self) -> list[str]:
        """Remove DONE*.md from the workspace before a run.

        The self-wake contract says the next agent reads a status file. If last
        run's files survive, an agent wakes on a status that describes a world
        that no longer exists -- a stale 'DONE' is worse than no signal, because
        it looks like progress. Returns the names removed.
        """
        removed = []
        for path in sorted(self.workspace.glob("DONE*.md")):
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
        return removed

    def _charge(self, n: int = 1) -> None:
        if self._requests + n > self.ceiling:
            raise TeamError(
                f"team run would exceed its request ceiling ({self.ceiling}); "
                f"{self._requests} already spent. Stopping before the spend.")
        self._requests += n

    def _settle(self, actual: int) -> None:
        """Reconcile the 1-request reservation with the completions that happened.

        ``_charge`` reserves one request before the send (a task may take
        several turns); this adds the remainder. It deliberately does NOT raise:
        by now the requests are already spent, and raising here would mark a
        task that actually succeeded as failed and throw away its result. The
        ceiling is instead enforced at the next task boundary, in _charge.
        """
        if actual > 1:
            self._requests += actual - 1

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
        llm = self.build_llm(role)
        prompt = compose_prompt(defn, extra_prompt)
        patched = defn.model_copy(update={"system_prompt": prompt})
        try:
            agent = spawn_agent(patched, llm, work_dir=str(self.workspace),
                                require=tuple(defn.tools or ()))
        except RoleFileError as exc:
            raise TeamError(f"role {name}: {exc}") from exc
        return agent, llm, lane

    def build_llm(self, role: str):
        """One LLM, resolved through this team's runtime (R5-D3).

        The engine's default runtime asks the router for the role's lane kwargs.
        teamkit's runtime overrides the model from its own `model` argument while
        keeping every other knob from the same lane, and resolves the credential
        against the lane's base URL rather than the first api_key in the config.
        """
        return self.runtime.build_llm(self, self.runtime.llm_kwargs(self, role))

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
    def _spec_path(self, override: str | Path | None) -> Path:
        return Path(override) if override else (self.workspace / "TEAM-SPEC.json")

    @staticmethod
    def _spec_start_stamp() -> float:
        """Freshness boundary for the spec file: monotone, wall-clock aligned.

        Compared against ``stat().st_mtime``, a wall-clock reading, so the two
        need a shared zero point: the monotone clock plus the wall-minus-monotone
        offset sampled at import. It therefore never moves backwards (an NTP step
        mid-run cannot un-stale a file the way a bare ``time.time()`` could) while
        still being meaningful against mtimes.
        """
        return time.monotonic() + _MONO_TO_WALL

    @staticmethod
    def _spec_signature(spec_path: Path) -> tuple | None:
        """Identity of the spec file right now: ``(mtime, size, content hash)``.

        None when there is no file. Two equal signatures mean nothing was
        written in between. The content hash is what makes the stale check safe
        on filesystems with coarse timestamps: a real rewrite changes the bytes
        even when the clock is too blunt to show a new mtime.
        """
        try:
            stat = spec_path.stat()
            digest = hashlib.sha256(spec_path.read_bytes()).hexdigest()
        except OSError:
            return None
        return (stat.st_mtime, stat.st_size, digest)

    def _spec_attempt(self, brief_path: Path, spec_path: Path) -> Task:
        """The architect's spec task, as ONE literal.

        The retry reuses this exact instruction rather than inventing a second
        phrasing: a re-ask that drifts is an opinion nobody can compare with the
        first attempt, and any timestamp or run id in it would break the frozen
        prompt prefix (rule 2).
        """
        return Task(
            id="spec", role="architect",
            instruction=(
                "Read the brief at " + str(brief_path) + ". Produce a numbered "
                "task spec for fixers and testers working in parallel in this "
                "workspace. WRITE THE SPEC TO THE FILE " + str(spec_path) + " as "
                "a JSON array of objects with keys: id, role, instruction, "
                "paths (files that task alone may touch), acceptance (commands "
                "that must exit 0), depends_on (ids of tasks that must finish "
                "before this one starts; omit or [] for none). Paths must be "
                "DISJOINT across tasks or the "
                "run will be refused. Then repeat the same array in a fenced "
                "json block in your reply. Do not edit any other file."),
            paths=(str(spec_path),), acceptance=())

    def _spec_is_fresh(self, spec_path: Path, started: float,
                       before: tuple | None) -> bool:
        """Does the spec file, as it stands, count as THIS step's work?

        Absent means no. So does a file that was already here before the step
        was sent, is unchanged since (same mtime, size and content), and predates
        the step: that is last run's leftover, not this architect's plan. The
        identity check is what protects a file the architect legitimately wrote,
        because filesystem timestamps are coarse: a real rewrite changes the
        bytes or the size even when the clock is too blunt to show a new mtime.
        """
        now = self._spec_signature(spec_path)
        if now is None:
            return False
        return not (before is not None and now == before and now[0] < started)

    def _spec_file_state(self, spec_path: Path, started: float,
                         before: tuple | None) -> tuple[list[Task], bool, str]:
        """Read and validate the spec FILE: ``(tasks, file_exists, reason)``.

        A file counts as a plan ONLY when it is fresh AND parses to at least one
        Task. The reason codes exist so ``report["spec_validation"]`` can still
        answer "why did that run ignore the file?" months later:

          missing      nothing at the spec path
          stale        the file was already here when the step started, is
                       unchanged since, and predates the step: last run's
                       leftovers, or another process's content. A stale spec is
                       a silent wrong plan, so its contents are never used.
          unreadable   present but not readable as text
          unparseable  not JSON, and no fenced JSON block in it either
          no-tasks     valid JSON that holds no tasks, so not a plan
          empty        a zero-byte file
          valid        one or more Tasks
        """
        if not spec_path.is_file():
            return [], False, "missing"
        if not self._spec_is_fresh(spec_path, started, before):
            return [], False, "stale"
        try:
            text = spec_path.read_text(encoding="utf-8")
        except Exception as exc:
            return [], True, f"unreadable:{type(exc).__name__}"
        try:
            rows = parse_spec_json(text)
        except Exception as exc:
            return [], True, f"unparseable:{type(exc).__name__}"
        if rows:
            return rows, True, "valid"
        if not text.strip():
            return [], True, "empty"
        # Nothing usable, and which kind matters: a file that is not JSON at
        # all, and a file of valid JSON holding no tasks, are different problems
        # for whoever reads the report.
        try:
            json.loads(text)
        except Exception:
            return [], True, "unparseable"
        return [], True, "no-tasks"

    def _spec_validation(self, spec_path: Path, started: float,
                         before: tuple | None, reply_text: str,
                         parse_reply: Callable[[str], list[Task]],
                         ) -> tuple[list[Task], str | None, dict[str, Any]]:
        """Resolve one spec attempt into ``(tasks, source, validation)``.

        File first, reply second, and the file only counts when it is fresh and
        parses to at least one Task. ``source`` stays None when both come up
        empty, which is the signal that the spec step has to be re-asked rather
        than a plan to run.
        """
        file_tasks, file_exists, reason = self._spec_file_state(
            spec_path, started, before)
        reply_tasks = parse_reply(reply_text)
        tasks, source = file_tasks, ("file" if file_tasks else None)
        if not tasks and reply_tasks:
            tasks, source = reply_tasks, "reply"
        validation = {"file_exists": file_exists,
                      "file_tasks": len(file_tasks),
                      "file_reason": reason,
                      "reply_tasks": len(reply_tasks)}
        return tasks, source, validation

    def message_for(self, task: Task) -> str:
        """The text a task is sent as. Default: the engine's own rendering.

        A facade run replaces it wholesale (`runtime.message_override`), because
        teamkit's callers hand over a brief + contract + memory block, not a
        rendered Task, and byte-compat on the message is their contract, not
        this engine's.
        """
        override = getattr(self.runtime, "message_override", None)
        if override is not None:
            return override + self.runtime.message_suffix
        return task.render() + self.runtime.message_suffix

    def run_task(self, task: Task) -> AgentResult:
        """Run one task through its role. This spends real requests."""
        role = task.role if task.role in self.defs else "fixer"
        if self.dry_run:
            # Still build the agent: that is where RC1/RC2/RC3 get caught. Only
            # the conversation is skipped, so a dry run exercises every check
            # that costs nothing and skips only the thing that costs money.
            agent, _llm, lane = self.build_agent(role)
            names = sorted({getattr(t, "name", str(t)) for t in agent.tools})
            if task.id == "spec" and not self._spec_done:
                # Seed the spec file so run_workflow exercises its real
                # file-parsing path, then clear it: leaving TEAM-SPEC.json
                # behind after a --dry-run would make the next "clean" checkout
                # differ from this one, and a stale spec is a silent wrong plan.
                self._spec_path(None).write_text(self.DRY_SPEC, encoding="utf-8")
                self._spec_done = True
                self._seeded_spec = True
            return AgentResult(id=task.id, role=role, ok=True, requests=0,
                               actions=["DRY-RUN"],
                               text=self.DRY_SPEC if task.id == "spec" else "",
                               summary=f"dry-run lane={lane.name} "
                                       f"model={lane.model} tools={names}")
        events: list[str] = []
        texts: list[str] = []
        try:
            prepared = self.runtime.prepare_task(self, task)
            if prepared is not None:
                # R5-D3: the runtime owns this conversation (teamkit's builder /
                # orchestrator single call). The engine still accounts for it:
                # same _charge, same ceiling, same record.
                conv, llm, summary = prepared
            else:
                from openhands.sdk import Conversation

                agent, llm, lane = self.build_agent(role)

                def on_event(event) -> None:
                    name = type(event).__name__
                    if name == "ActionEvent":
                        action = getattr(event, "action", None)
                        events.append(type(action).__name__)
                        # FinishAction carries the reply in .message. An acting
                        # turn does not also emit a MessageEvent with
                        # source=agent, so without this the agent's final text is
                        # invisible. A live run showed the symptom: the architect
                        # spent 6 requests and produced a FileEditorAction, yet
                        # the report recorded text_len=0: the reply-fallback had
                        # nothing to read, so a 6-request architect step
                        # contributed nothing to the run. run_workflow did warn
                        # and skip fixers rather than claim success, but the reply
                        # itself was still unrecoverable.
                        message = getattr(action, "message", None)
                        if isinstance(message, str) and message.strip():
                            texts.append(message)
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
                summary = f"lane={lane.name} model={lane.model}"
            self._charge()                       # reserve 1 request before send
            conv.send_message(self.message_for(task))
            conv.run()
            # One usage entry per completion: that is the real number of paid
            # requests, which is what this plan bills (not tokens, not tasks).
            metrics = getattr(llm, "metrics", None)
            calls = len(getattr(metrics, "token_usages", []) or []) or 1
            self._settle(calls)
            self.router.record(self.role_for(role), calls)
            return AgentResult(id=task.id, role=role, ok=True, requests=calls,
                               actions=events, text="".join(texts),
                               summary=summary)
        except TeamError:
            raise
        except Exception as exc:
            if self.runtime.propagate_task_errors:
                raise
            return AgentResult(id=task.id, role=role, ok=False, actions=events,
                               text="".join(texts),
                               error=f"{type(exc).__name__}: {str(exc)[:200]}")

    def map_agents(self, tasks: list[Task]) -> list[AgentResult]:
        """Run independent tasks concurrently, in dependency-ordered waves.

        Tasks are grouped into waves by their ``depends_on`` ids; everything in
        one wave runs in parallel, then the next wave starts. Without this, a
        task that needs another's output could run first and "pass" against a
        file that does not exist yet.

        Concurrency is capped by the lane's ``concurrency_limit``. The plan bills
        requests against a rolling window, so N agents firing simultaneously is
        N simultaneous quota hits — parallelism here is a latency win, not free
        throughput.
        """
        if not tasks:
            return []
        assert_disjoint(tasks)
        results: list[AgentResult] = []
        for wave in order_waves(tasks):
            lanes = [self.router.lane_for(self.role_for(t.role
                         if t.role in self.defs else "fixer")) for t in wave]
            workers = min(len(wave), max(l.concurrency_limit for l in lanes))
            if workers <= 1:
                wave_results = [self.run_task(t) for t in wave]
            else:
                with cf.ThreadPoolExecutor(max_workers=workers) as pool:
                    wave_results = list(pool.map(self.run_task, wave))
            failed = [r.id for r in wave_results if not r.ok]
            if failed:
                for r in wave_results:
                    self.results.append(r)
                raise TeamError(
                    f"task(s) {failed} failed; refusing to run dependent waves. "
                    "A dependent task built on a broken base reports success "
                    "about a world that does not exist.")
            results.extend(wave_results)
        self.results.extend(results)
        return results

    def run_workflow(self, brief_path: str | Path, *,
                     spec_parser: Callable[[str], list[Task]] | None = None,
                     spec_file: str | Path | None = None,
                     on_run: Callable[[str], None] | None = None,
                     ) -> dict[str, Any]:
        """One engine, two shapes, and (R5-D1) a graded tail.

        Architect mode is the proven flow: spec -> disjoint parallel fixers ->
        reviewer. Builder mode (`runtime.direct_task`, what teamkit's
        `roles='none'` maps to) runs ONE task directly through the same
        accounting. Both shapes then go through the SAME post-run grading stage:
        protected-path integrity, component gate, optional pair gate against the
        run-start baseline, outcome split, and a run record.

        `on_run` is a test seam, called at the point where a live team has just
        finished editing the workspace and before the work evidence is
        fingerprinted, exactly where teamkit's own runs put conv.run().
        """
        self._begin_run()
        if self.dry_run:
            report = self._run_architect_workflow(
                brief_path, spec_parser=spec_parser, spec_file=spec_file)
        elif isinstance(self.runtime.direct_task, Task):
            report = self._run_direct(self.runtime.direct_task)
        else:
            report = self._run_architect_workflow(
                brief_path, spec_parser=spec_parser, spec_file=spec_file)
        if self.dry_run:
            return report
        return self._grade_run(report, on_run=on_run)

    # -- R5-D1: the graded tail -----------------------------------------
    def _begin_run(self) -> None:
        """Everything that must happen before the first request can be spent.

        The fingerprints are taken HERE, not at grading time: the bar the work is
        measured against is the world as the run found it, so a team that commits
        cannot move it. `capture_workspace_state` is read-only (no stash, no
        dangling object) and runs on every path; the `stash create` ref is only
        taken when a pair check will actually use it.
        """
        ws = str(self.workspace)
        self.baseline = capture_workspace_state(ws)
        self.baseline_ref = None
        if (self.contract or {}).get("incumbent_gate"):
            try:
                self.baseline_ref = capture_baseline_ref(ws)
            except Exception:
                self.baseline_ref = None
        iter_cap = getattr(self.runtime, "iter_cap", None) or max(
            1, int(self.ceiling) * 3)
        if not self.dry_run:
            self.agent_summary = self.runtime.preflight(self, iter_cap)

    def _grade_run(self, report: dict[str, Any], *,
                   on_run: Callable[[str], None] | None = None) -> dict[str, Any]:
        """The post-workflow grading stage. No agent prose is ever evidence."""
        ws = str(self.workspace)
        if on_run:
            on_run(ws)
        # Fingerprints BEFORE the gate: a gate that runs a test suite creates
        # __pycache__/.pytest_cache dirt of its own, and crediting the team with
        # it would relabel a stalled run as partial_work.
        self.after_team = capture_workspace_state(ws)
        if not self.grade:
            report["graded"] = False
            return report
        graded = evaluate_grade(ws, self.contract, tasks=self.tasks,
                                baseline=self.baseline, after=self.after_team,
                                baseline_ref=self.baseline_ref,
                                declared_paths=list(getattr(
                                    self.runtime, "declared_paths", ()) or ()))
        report.update({
            "graded": True,
            "status": graded["status"],
            "gate": graded["gate"],
            "gate_tail": graded["gate_tail"],
            "pair": graded["pair"],
            "outcome": graded["outcome"],
            "work_evidence": graded["work_evidence"],
            "protect": graded.get("protect"),
            "violations": graded.get("violations") or [],
        })
        if graded.get("reason"):
            report["reason"] = graded["reason"]
        if graded.get("retained") is not None:
            # v2 runs only: what was kept and why, for retained.json (R1).
            report["retained"] = graded["retained"]
        report["run_dir"] = self._write_record(report, graded)
        return report

    def _write_record(self, report: dict[str, Any],
                      graded: dict[str, Any]) -> str | None:
        """A run record per graded run, into the ONE runs/ tree both engines use.

        teamkit passes record=False and writes the record itself, because it owns
        the per-slug lock; the CLI passes nothing and gets a plain record here.
        """
        if self._record is False:
            return None
        try:
            root = runs_root(self.runs_root)
            slug = run_slug(self.workspace) or "workspace"
            run_dir = unique_run_dir(root, slug)
            config = {"run_dir": run_dir, "workspace": str(self.workspace),
                      "ceiling": self.ceiling, "contract": self.contract,
                      "dry_run": self.dry_run,
                      "brief_sha256": _sha(str(report.get("spec_file") or "")),
                      "requests_charged": report.get("requests_charged"),
                      "engine": "team",
                      "api_key_source": self.runtime.record_api_key()}
            if getattr(self, "agent_summary", None):
                config["agents"] = self.agent_summary
            if getattr(self.runtime, "recall", None):
                config["recall"] = self.runtime.recall
            state = {name: {"status": "done"} for name in
                     ("spec", "team_run", "gate", "lessons")}
            if report.get("aborted"):
                state["team_run"] = {"status": "fail"}
            return write_run_record(run_dir, report, config=config, state=state,
                                    retained=graded.get("retained"))
        except Exception:
            return None

    def _run_direct(self, task: Task) -> dict[str, Any]:
        """Builder mode (R5-D3): one task, no architect, same accounting.

        The task the caller built IS the spec, so tasks_parsed is 1 and
        spec_source says 'direct' rather than pretending a file was read.
        """
        stale = self.clear_stale_status_files()
        report: dict[str, Any] = {"steps": [], "requests_charged": 0,
                                  "spec_file": None,
                                  "cleared_stale_status_files": stale,
                                  "spec_source": "direct", "spec_attempts": 0,
                                  "tasks_parsed": 1}
        self.tasks = [task]
        assert_disjoint([task])
        result = self.run_task(task)
        report["steps"].append(self._as_dict(result))
        if not result.ok:
            report["aborted"] = f"task {result.id} failed: {result.error}"
        report["status_files"] = sorted(p.name
                                        for p in self.workspace.glob("DONE*.md"))
        report["requests_charged"] = self._requests
        return report

    def _run_architect_workflow(self, brief_path: str | Path, *,
                     spec_parser: Callable[[str], list[Task]] | None = None,
                     spec_file: str | Path | None = None,
                     ) -> dict[str, Any]:
        """architect spec -> parallel fixers -> reviewer, with accounting.

        The architect is asked to WRITE ITS SPEC TO A FILE, and we read that file
        first. Parsing chat text is the fallback, not the primary path: an agent
        can complete a run with no final text event at all (observed in this
        repo's own probes), which would silently yield zero tasks. A file either
        exists with valid JSON in it or it does not — there is no quiet middle.

        Validation is explicit, because two live runs showed the quiet middle
        anyway: the architect wrote a spec, ended its turn with a few characters
        of reply, and the old flow fell through file-then-reply into "no
        parseable task spec" while the reviewer carried on grading nothing. So
        the file is validated first (it counts ONLY if it parses to >= 1 Task and
        is not stale), the reply is the fallback, and when both yield zero the
        spec step is re-asked ONCE through the normal accounting path. Every run
        records ``report["spec_validation"]`` (what each source yielded) and
        ``report["spec_attempts"]`` (1 or 2), next to the existing
        ``spec_source`` / ``tasks_parsed`` keys.

        ``spec_parser`` turns free text into Tasks when the file is absent; it
        defaults to :func:`parse_tasks`, which reads a fenced JSON block. Pass
        something stricter for production: a loosely parsed spec is how two
        fixers end up owning the same file.
        """
        brief_path = Path(brief_path)
        spec_path = self._spec_path(spec_file)
        stale = self.clear_stale_status_files()
        report: dict[str, Any] = {"steps": [], "requests_charged": 0,
                                  "spec_file": str(spec_path),
                                  "cleared_stale_status_files": stale}
        parse_reply = spec_parser or parse_tasks
        # Freshness boundary for the stale-file guard: ONE stamp, taken before
        # the first spec step is sent and reused for every validation in this
        # run, paired with the file's signature at the same moment. A
        # TEAM-SPEC.json left by an earlier run predates the stamp and is
        # unchanged since, so it is not this architect's plan however valid its
        # JSON; a file the architect writes during the step is newer or differs.
        started = self._spec_start_stamp()
        before = self._spec_signature(spec_path)
        attempts = 0
        spent: list[int] = []

        def spec_step() -> AgentResult:
            """Send the spec step, accounted through run_task like any other.

            Each call appends one entry to ``report["steps"]`` and spends
            through run_task -> _charge: the retry is an ordinary step, not a
            bypass, so it cannot quietly get past the request ceiling.
            """
            nonlocal attempts
            before_requests = self._requests
            attempts += 1
            result = self.run_task(self._spec_attempt(brief_path, spec_path))
            report["steps"].append(self._as_dict(result))
            spent.append(self._requests - before_requests)
            return result

        def resolve(result: AgentResult) -> list[Task]:
            """Validate file-then-reply for the latest attempt and record it."""
            tasks, src, validation = self._spec_validation(
                spec_path, started, before, result.text, parse_reply)
            report["spec_validation"] = validation
            report["spec_attempts"] = attempts
            report["spec_requests_spent"] = spent
            report["spec_source"] = src
            report["tasks_parsed"] = len(tasks)
            if validation["file_reason"] not in ("valid", "missing"):
                report["warnings"] = (report.get("warnings") or []) + [
                    f"spec file present but {validation['file_reason']}; its "
                    "contents were ignored rather than run as a plan"]
            return tasks

        attempt = spec_step()
        tasks = resolve(attempt)
        if not attempt.ok:
            report["aborted"] = "architect failed: " + str(attempt.error)
            report["requests_charged"] = self._requests
            return report
        if not tasks:
            # Both sources empty on the first pass: re-ask ONCE. Same
            # instruction, same accounting -- run_task -> _charge -- so the
            # retry cannot slip past the request ceiling any more than any
            # other step can. A second failure keeps the previous behaviour
            # exactly: warn loudly, skip fixers, let the reviewer run anyway.
            tasks = resolve(spec_step())
        self.tasks = tasks
        if not tasks:
            # Loud, never silent: an architect that produced no task list is a
            # failed step, and skipping fixers quietly would let the run report
            # success having built nothing.
            report["warnings"] = (report.get("warnings") or []) + [
                "architect produced no parseable task spec; fixers were skipped"]
        else:
            report["steps"].extend(self._as_dict(r)
                                   for r in self.map_agents(tasks))

        review = self.run_task(Task(
            id="review", role="reviewer",
            instruction=(
                "Verify each acceptance gate in this workspace by running it. "
                "Read every per-task status file matching DONE*.md, then report "
                "PASS or FAIL per task with the command output that justifies "
                "it. Write your verdict to DONE.md (you are the only agent "
                "allowed to use that name). If a status file reports a gate it "
                "could not run, say so rather than marking it passed."),
            paths=("DONE.md",), acceptance=("python3 -m pytest -q",)))
        report["steps"].append(self._as_dict(review))
        report["status_files"] = sorted(p.name for p in self.workspace.glob("DONE*.md"))
        report["requests_charged"] = self._requests
        if self.dry_run and self._seeded_spec:
            try:
                self._spec_path(None).unlink()
                report["spec_file"] = str(self._spec_path(None)) + " (seeded then removed)"
            except OSError:
                pass
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
def order_waves(tasks: list[Task]) -> list[list[Task]]:
    """Group tasks into dependency-ordered waves; wave N runs after wave N-1.

    Raises on an unknown dependency or a cycle. Refusing is deliberate: an
    unresolvable spec silently reorders into "everything at once", which means a
    fixer builds on a file that another fixer has not written yet.
    """
    by_id = {t.id: t for t in tasks}
    if len(by_id) != len(tasks):
        raise TeamError("duplicate task ids in spec; parallel agents would "
                        "collide on the same identity")
    remaining = dict(by_id)
    done: set[str] = set()
    waves: list[list[Task]] = []
    while remaining:
        ready = [t for t in remaining.values()
                 if all(dep in done for dep in t.depends_on)]
        if not ready:
            unresolved = {t.id: [d for d in t.depends_on if d not in by_id]
                          for t in remaining.values()}
            unknown = {k: v for k, v in unresolved.items() if v}
            if unknown:
                raise TeamError(f"tasks depend on unknown ids: {unknown}")
            raise TeamError(f"dependency cycle among {sorted(remaining)}")
        for t in ready:
            remaining.pop(t.id)
            done.add(t.id)
        waves.append(ready)
    return waves


#: Acceptance commands are REPORTED, never executed by this module. They come
#: from model output, and shelling out to model-authored strings is how a
#: "review" step becomes an arbitrary-code-execution step. A human or the
#: reviewer agent runs them; the orchestrator only records them.
def _rows_to_tasks(data: Any) -> list[Task]:
    """Coerce parsed JSON into Tasks. Returns [] rather than a partial spec."""
    if isinstance(data, dict):
        data = data.get("tasks") or []
    if not isinstance(data, list):
        return []
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
    return tasks


def parse_spec_json(text: str) -> list[Task]:
    """Parse the architect's spec FILE: strict JSON first, fenced-block second.

    Returns [] on anything unrecognised. A half-read spec is worse than none,
    because it silently drops work the reviewer will then grade as missing.
    """
    if not text or not text.strip():
        return []
    try:
        return _rows_to_tasks(json.loads(text))
    except json.JSONDecodeError:
        pass
    return parse_tasks(text)          # tolerate a fenced block inside the file


def parse_tasks(text: str) -> list[Task]:
    """Extract a JSON task array from an architect's reply.

    Tolerates prose around the block and a top-level {"tasks": [...]} wrapper,
    because models do both. Anything unparseable returns [] rather than a
    half-read spec.
    """
    if not text:
        return []
    candidates = re.findall(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", text, re.S)
    candidates += re.findall(r"(\[\s*\{.*\}\s*\])", text, re.S)
    for blob in candidates:
        try:
            tasks = _rows_to_tasks(json.loads(blob))
        except json.JSONDecodeError:
            continue
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
    ap.add_argument("--dry-run", action="store_true",
                    help="walk the whole workflow (architect, spec parse, "
                         "overlap check, reviewer) but send 0 requests")
    ap.add_argument("--lanes", action="store_true",
                    help="print the router lane table; spends 0 requests")
    ap.add_argument("--max-requests", type=int, default=None,
                    help="hard ceiling for this run (must be <= router ceiling)")
    ap.add_argument("--gate", default=None,
                    help="component gate command run in the workspace AFTER the "
                         "workflow; grading is what decides success, not prose")
    ap.add_argument("--incumbent-gate", default=None,
                    help="pair check: the same gate run against the run-start "
                         "baseline, so improvement (not just green) passes")
    # default=None, NOT []: argparse's append mutates the default list object,
    # and main() is called repeatedly in one process by the CLI tests.
    ap.add_argument("--protect", action="append", default=None, metavar="PATH",
                    help="protect this path: a run that changes it without "
                         "declaring it fails the gate regardless of exit code "
                         "(repeatable)")
    ap.add_argument("--no-record", action="store_true",
                    help="skip the runs/ record for this CLI run")
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

    contract: dict[str, Any] = {}
    if args.gate:
        contract["gate"] = args.gate
    if args.incumbent_gate:
        contract["incumbent_gate"] = args.incumbent_gate
    if args.protect:
        contract["protect"] = list(args.protect)
    contract = contract or None

    try:
        team = Team(args.workspace, router=router, roles_dir=args.roles_dir,
                    max_requests=args.max_requests, dry_run=args.dry_run,
                    contract=contract, record=False if args.no_record else None)
    except TeamError as exc:
        print(f"REFUSED BEFORE SPEND: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if args.dry_run:
        if not args.brief:
            print("REFUSED: --dry-run needs --brief PATH", file=sys.stderr)
            return 2
        report = team.run_workflow(args.brief)
        print(json.dumps(report, indent=2, default=str))
        spent = report["requests_charged"]
        print(f"\nDRY RUN: steps={len(report['steps'])} "
              f"tasks_parsed={report.get('tasks_parsed')} "
              f"spec_source={report.get('spec_source')} "
              f"spec_attempts={report.get('spec_attempts')} "
              f"spec_validation={report.get('spec_validation')} "
              f"requests={spent}")
        if report.get("warnings"):
            for w in report["warnings"]:
                print("WARNING: " + w)
        if spent != 0:
            print("BUG: a dry run spent requests", file=sys.stderr)
            return 1
        return 1 if report.get("aborted") or report.get("warnings") else 0

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
          f"requests_charged={report['requests_charged']} ceiling={team.ceiling}"
          + (f" status={report.get('status')} outcome={report.get('outcome')}"
             f" run_dir={report.get('run_dir')}" if report.get("graded") else ""))
    if report.get("warnings"):
        for w in report["warnings"]:
            print("WARNING: " + w)
    if report.get("reason"):
        print("REASON: " + str(report["reason"]))
    if report.get("graded"):
        return grade_exit_code(report.get("status"),
                               aborted=bool(report.get("aborted")),
                               failed_steps=len(failed))
    return 1 if failed or report.get("aborted") else 0


if __name__ == "__main__":
    raise SystemExit(main())
