"""teamkit — Hermes↔OpenHands structured bridge.

Usage (taskmaster side):
    from teamkit import run_team
    result = run_team(
        brief="/home/hermes/.hermes/qwen-tdam-pr-brief.md",   # or inline spec text
        workspace="/home/hermes/projects/foo",
        budget_calls=80,
        contract={"deliverables": ["src/x.py","tests/test_x.py"], "gate": "pytest -q tests/"},
    )
    # result: {status, calls, cache_hit_pct, verdict:{pass:[],fail:[]}, files_changed:[], tail}
Never parses human prose for verdicts: gate command exit code + git diff are the truth.

Four runner disciplines (R1 + R2, ported from Gen-Verse/RSI-Anything hygiene
ideas):
  D1 RunRecord  every run persists runs/<utc>-<slug>/{config,state,summary}.json,
                guarded by one O_EXCL lock per run slug (FileLockError, pre-spend).
  D2 Gate v2    contract['incumbent_gate'] turns the single gate into a component
                check (workspace) + a pair check against a pristine baseline
                materialized outside runs/ -> GATE_PASS_NEW / GATE_PASS_TIE /
                GATE_FAIL, and retained.json says what was kept.
  D3 Chokepoint budget_calls > 120 -> BUDGET_REFUSED with zero side effects;
                TEAMKIT_DRY_RUN=1 -> DRY_PLAN, record + stdout plan, zero API calls.
  D4 Outcome    every record carries `outcome`: success | partial_work |
                abstained | refused | tie_kept. It separates "the system failed"
                (gate failed after real edits) from "the system did nothing"
                (N minutes, zero commits, zero tracked diff) so lane stalls are
                not counted as gate failures. See README.
Stdlib only, and openhands stays a lazy import so unit tests run on bare python3.
"""
import hashlib, json, logging, os, re, shutil, subprocess, sys, tempfile, time

#: R4b-D7: the RC2 guard used to be an assert inside preflight(). Loud does not
#: mean fatal: a poisoned `model:` line is repaired in memory and reported here,
#: so the run keeps going with the inherited lane. logger.warning over print:
#: pytest's caplog and any host handler both see it, a bare stdout line does not.
log = logging.getLogger('teamkit')

CFG = '/home/hermes/.hermes/config.yaml'
TP_ANTH = 'https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic'
TP_COMPAT = 'https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1'
AGENTS_DIR = '/home/hermes/.hermes/cache/scratch/oh_team2/agents'
# R4 import-path decision: teamkit loads the REPO copy below, so the code that
# writes shared memory travels with the history that explains it. The executor-
# readable twin at /home/hermes/.hermes/scripts/tdai-bridge.py is kept
# byte-identical (`diff` clean) for anything that runs outside this repo.
TDAI_BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'tdai-bridge.py')
TDAI_BRIDGE_EXECUTOR = '/home/hermes/.hermes/scripts/tdai-bridge.py'
#: R4-D3: only these outcomes may auto-seed lessons into the shared hub.
SEEDABLE_OUTCOMES = ('success', 'tie_kept')
#: R4-D4: the only recall_status values, so four outcomes stay distinguishable.
RECALL_STATUSES = ('fired', 'empty', 'unavailable', 'error')

# Model-lane policy (mechanical preflight, owner rule 2026-10-11):
# anthropic-prefix flash/ds models die in-loop with empty tool-less responses
# (confirmed twice: netflow deepseek 2026-10-05, bcnbus qwen3.8-flash 2026-10-11).
_INLOOP_BLOCKED_ON_ANTHROPIC = ('qwen3.8-flash', 'deepseek-v4')


def _lane_check(model, base_url):
    """Return corrected (model, base_url) or raise before spending calls."""
    if TP_ANTH in base_url and any(b in model for b in _INLOOP_BLOCKED_ON_ANTHROPIC):
        raise ValueError(
            f"LANE GUARD: model '{model}' on anthropic route is a confirmed in-loop "
            "empty-response killer; use compatible-mode base_url (openai/ prefix) or "
            "a thinking-native model (qwen3.8-max) on anthropic. See openhands-sdk-pilot skill.")
    if TP_COMPAT in base_url and not model.startswith('openai/'):
        model = 'openai/' + model.split('/')[-1]
    return model, base_url


def _tdai():
    """Optional TDAI memory-hub bridge (read/write paths). No key -> None:
    runs proceed exactly as before (zero regression). Never raises.

    R4-D4: a bridge that loaded but whose hub call threw is NOT this function's
    business — it returns the module, and recall_detail()'s status carries the
    difference between "no memory" and "memory unreachable".
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('tdai_bridge', TDAI_BRIDGE)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod if mod.available() else None
    except Exception:
        return None


def _recall_info(tb, query):
    """R4-D4: run the read path ONCE and describe the result, four ways.

    -> {'recall_status': fired|empty|unavailable|error,
        'recall_hits': [{'name'|'sha8', 'chars'}],
        'hits': [content strings for the message]}
    `recall_hits` is names/hashes and sizes only, never content, so it is safe
    to put in config.json. A hub that answered with an error dict used to no-op
    into the same silence as a hub with nothing to say; that conflation is
    exactly what this returns a label for.
    """
    if not tb:
        return {'recall_status': 'unavailable', 'recall_hits': [], 'hits': []}
    try:
        d = tb.recall_detail(query)
    except Exception as exc:
        return {'recall_status': 'error', 'recall_hits': [], 'hits': [],
                'recall_error': f'{type(exc).__name__}: {str(exc)[-200:]}'}
    hits = [{'name': h['name'], 'chars': h['chars']} if 'name' in h
            else {'sha8': h.get('sha8', ''), 'chars': h.get('chars', 0)}
            for h in d.get('meta') or []]
    status = d.get('status') or 'empty'
    if status == 'fired' and not (d.get('hits') or []):
        status = 'empty'   # `fired` with nothing to show is a contradiction
    if status not in RECALL_STATUSES:
        # An unlabelled answer is not a documented one: report it as `error`
        # rather than letting it masquerade as the `empty` it is not.
        status = 'error'
    return {'recall_status': status,
            'recall_hits': hits, 'hits': d.get('hits') or [],
            'recall_error': (d.get('error') or '')[:200] or None}


def _recall_block(query, info=None):
    """The PRIOR LEARNINGS text appended to the brief, plus its attribution.

    Returns (text, info): the text lands in the message AFTER brief hashing, so
    config.json can only know what memory contributed if the caller records the
    info dict (R4-D4). `info` may be passed in so a caller that already ran the
    read path does not call the hub twice.
    """
    info = info if info is not None else _recall_info(_tdai(), query)
    hits = info.get('hits') or []
    if info.get('recall_status') != 'fired' or not hits:
        return '', info   # the block only ever lands on a real, fired recall
    return ("\n\nPRIOR LEARNINGS (shared team memory; treat as hints, "
            "verify against repo):\n" + "\n---\n".join(hits)) + "\n", info


def learn_out(workspace, notes):
    """Post-run distillation into the shared hub. notes: list[str] of durable lessons.

    R4-D1: names come from lesson CONTENT (`lesson-<ws-slug>-<sha8>`), not a
    slot index, so every note in a run is a distinct record and a re-run of the
    same lesson is the hub's duplicate rejection rather than a silent no-op.
    R4-D4: returns seeded/duplicates/errors/of so the caller can mark a partly
    landed batch `partial` instead of `done`.
    """
    tb = _tdai()
    if not tb:
        return {'tdai': 'unavailable', 'seeded': 0, 'duplicates': 0,
                'errors': 0, 'of': len(notes or [])}
    if hasattr(tb, 'seed_lessons'):
        return tb.seed_lessons(workspace, notes)
    names, seeded = [], 0
    for n in notes or []:
        nm = (tb.lesson_name(workspace, n) if hasattr(tb, 'lesson_name')
              else f'teamkit-lesson-{len(names)}')
        names.append(nm)
        seeded += 1 if tb.seed_skill(nm, n) else 0
    return {'names': names, 'seeded': seeded, 'duplicates': 0,
            'errors': len(notes or []) - seeded, 'of': len(notes or [])}



def _key():
    m = re.search(r"^  api_key: (.*)$", open(CFG).read(), re.M)
    return m.group(1).strip().strip("'").strip('"')


def _known_tool_names():
    """Flat set of tool names the SDK preset actually registers."""
    from openhands.tools.preset.default import get_default_tools
    flat = set()
    for ts in get_default_tools(enable_browser=False, enable_sub_agents=True):
        for t in getattr(ts, 'tools', [ts]):
            flat.add(getattr(t, 'name', str(t)))
    return flat


def _normalize_model(d):
    """R4b-D7: the RC2 guard, self-healing. (name, original) or (None, None).

    A `model:` value that is not ''/inherit is an LLM *profile name* to the SDK
    (registry.agent_definition_to_factory -> LLMProfileStore.load), and a profile
    that does not exist raises inside every Task call — mid-run, after spend. The
    file stays on disk exactly as its author left it; the in-memory definition
    used by THIS run becomes 'inherit', and the warning names file + value so the
    owner can fix the source. Returns (name, original) when something was
    normalized so the caller can record it.
    """
    m = getattr(d, 'model', None)
    if isinstance(m, str) and m.strip() not in ('', 'inherit'):
        source = getattr(d, 'source', None) or '<no source recorded>'
        log.warning("TDAI/RC2 normalize: role '%s' file %s sets model '%s' "
                    "(an LLM profile name the SDK would look up and crash on); "
                    "using 'inherit' for this run. Fix the file to persist a "
                    "profile.", d.name, source, m)
        try:
            d.model = 'inherit'
        except Exception:
            try:                # frozen/pydantic-strict models: copy instead
                object.__setattr__(d, 'model', 'inherit')
            except Exception:
                return d.name, m
        return d.name, m
    return None, None


def workspace_agent_dirs(workspace):
    """The SDK's own project-level agent dirs, resolved against `workspace`.

    `_FILE_BASED_AGENTS_DIR` in openhands/sdk/subagent/load.py is literally
    ['.agents/agents', '.openhands/agents'], relative to the conversation's
    working dir — so these two paths are exactly what LocalConversation would
    auto-register on the first send_message(). Keeping the list in one place is
    what lets builder mode and orchestrator mode share one refresh.
    """
    from pathlib import Path
    ws = Path(os.path.abspath(str(workspace)))
    return [ws / '.agents' / 'agents', ws / '.openhands' / 'agents']


def _clamp_cap(d, cap):
    """R4b-D7b: effective per-role iteration cap, def value bounded by the run.

    Returns the value the definition now carries: min(def, cap) when both exist
    (def wins lower, the budget clamps a permissive frontmatter), the cap alone
    when the file states none, and the def alone when `cap` is None.
    """
    declared = getattr(d, 'max_iteration_per_run', None)
    if isinstance(cap, int) and cap > 0:
        eff = min(int(declared), cap) if isinstance(declared, int) else cap
    else:
        eff = declared
    try:
        d.max_iteration_per_run = eff
    except Exception:
        try:
            object.__setattr__(d, 'max_iteration_per_run', eff)
        except Exception:
            pass
    return eff


def refresh_workspace_agents(workspace, defs=None, cap=None):
    """R4b-D7: force the registry to hold CURRENT disk definitions (both modes).

    Why this is not the SDK's job — three facts, each verified offline against
    openhands-sdk 1.54 (tests/test_r4b_d7_agents.py, probe finding recorded
    there):
      1. LocalConversation DOES auto-register {working_dir}/.agents/agents and
         {working_dir}/.openhands/agents at the first send_message()/run(), and
         LocalWorkspace keeps `working_dir` verbatim (no resolve, no normalise),
         so a relative workspace points the SDK somewhere else entirely.
      2. It registers with `register_agent_if_absent`: an entry from an earlier
         run in the same process is kept, so the second run silently executes
         the FIRST run's file contents. Stale drift.
      3. The lazy pass never re-reads a file it has already registered.
    So teamkit pops the name and re-registers from the file it just parsed: the
    registry after this call is exactly what is on disk right now.

    `cap` (R4b-D7b) clamps each definition's `max_iteration_per_run` in memory.
    The SDK spawns sub-agent conversations with
    `factory.definition.max_iteration_per_run or parent.max_iteration_per_run`
    (openhands/tools/task/manager.py::_create_task), so clamping the definition
    IS what bounds the role's Conversation, and `permission_mode` reaches that
    child through the same definition (get_confirmation_policy()). A permissive
    frontmatter value can therefore never outrun the run budget.

    Returns ({'names': [...], 'normalized': [{'name','model'}],
             'caps': {name: effective_cap}, 'defs': [...]}, warnings).
    `defs` is what the orchestrator registration loop consumes. Never raises:
    an unloadable file is already a loud SDK warning, and a refresh failure must
    not abort a run that has not spent anything yet.
    """
    from pathlib import Path
    from openhands.sdk.subagent import (agent_definition_to_factory,
                                        load_agents_from_dir,
                                        register_agent_if_absent)
    from openhands.sdk.subagent.registry import _agent_factories, _registry_lock

    os.environ['OPENHANDS_SUPPRESS_BANNER'] = '1'
    if defs is None:
        defs = []
        for agents_dir in workspace_agent_dirs(workspace):
            try:
                defs.extend(load_agents_from_dir(Path(agents_dir)))
            except Exception as exc:
                log.warning("agent dir %s failed to load: %s: %s", agents_dir,
                            type(exc).__name__, str(exc)[-200:])
    registered, normalized, caps, warnings = [], [], {}, []
    seen, unique = set(), []
    for d in defs or []:
        if d.name in seen:
            continue        # first dir wins, mirroring the SDK's dedup order
        seen.add(d.name)
        unique.append(d)
    defs = unique
    for d in defs:
        try:
            nm, original = _normalize_model(d)
            if nm:
                normalized.append({'name': nm, 'model': original})
                warnings.append(f"model normalized: {nm} -> inherit "
                                f"(was '{original}')")
            caps[d.name] = _clamp_cap(d, cap)
            with _registry_lock:
                # force-refresh: drop any stale registration from a previous run
                # in this process so the CURRENT md file is what executes
                _agent_factories.pop(d.name, None)
            # The DEFINITION object is passed as the description, not a string:
            # register_agent_if_absent -> _resolve_agent_definition turns a plain
            # string into a metadata-empty AgentDefinition, and the Task manager
            # reads `factory.definition.max_iteration_per_run` +
            # `get_confirmation_policy()` off exactly that object. A string here
            # is how a role's cap and permission_mode silently stop existing.
            register_agent_if_absent(d.name,
                                     agent_definition_to_factory(d, workspace),
                                     d)
            registered.append(d.name)
        except Exception as exc:
            log.warning("agent '%s' could not be registered: %s: %s",
                        getattr(d, 'name', '?'), type(exc).__name__,
                        str(exc)[-200:])
            warnings.append(f"register failed: {getattr(d, 'name', '?')}: "
                            f"{type(exc).__name__}")
    return ({'names': registered, 'normalized': normalized, 'caps': caps,
             'defs': defs or []}, warnings)


def _workspace_defs(workspace):
    """Parse (never register) the workspace's own file agents.

    refresh_workspace_agents() consumes these alongside the AGENTS_DIR roles so
    one call owns every registry mutation; failures degrade to "no extra defs"
    the same way the refresh's own scan does.
    """
    from pathlib import Path
    try:
        from openhands.sdk.subagent import load_agents_from_dir
    except Exception:
        return []
    out = []
    for agents_dir in workspace_agent_dirs(workspace):
        try:
            out.extend(load_agents_from_dir(Path(agents_dir)))
        except Exception as exc:
            log.warning("agent dir %s failed to load: %s: %s", agents_dir,
                        type(exc).__name__, str(exc)[-200:])
    return out


def preflight():
    """Skill rule: verify role defs parse with registered tools BEFORE spending calls.

    R4b-D7: the RC2 `model:` check is no longer an assert here — it is
    _normalize_model(), which repairs the in-memory definition and warns loudly.
    A typo in one role file is a repairable defect, not a reason to refuse the
    whole run at zero spend.
    """
    from pathlib import Path
    from openhands.sdk.subagent import load_agents_from_dir
    flat = _known_tool_names()
    defs = load_agents_from_dir(Path(AGENTS_DIR))
    for d in defs:
        tl = list(d.tools)
        assert tl, f"PREFLIGHT FAIL {d.name}: zero tools (frontmatter?)"
        miss = [x for x in tl if x not in flat]
        assert not miss, f"PREFLIGHT FAIL {d.name}: unregistered {miss}"
        _normalize_model(d)
    return defs


# ---------------------------------------------------------------- D1: RunRecord
class FileLockError(RuntimeError):
    """Raised when another live runner already holds this run slug's lock.

    Refusal happens BEFORE any API call is spent: that is the whole point.
    """


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS_DIR = os.environ.get('TEAMKIT_RUNS_DIR') or os.path.join(REPO_ROOT, 'runs')
GATE_TIMEOUT = 900
BUDGET_MAX_CALLS = 120
STAGES = ('preflight', 'team_run', 'gate', 'lessons')


class _Ctx:
    """Per-run recording context: lock + recorder + stage ledger + baseline ref.

    `baseline` (D4) is the read-only run-start fingerprint used to tell a failed
    gate from a run that touched nothing; `baseline_ref` (D2) is the stash/HEAD
    commit the pair check materializes. They are deliberately separate: the
    fingerprint must exist for EVERY run, including single-gate ones.
    """

    def __init__(self):
        self.recorder = None
        self.lock_path = None
        self.run_dir = None
        self.baseline_ref = None
        self.baseline = None
        self.workcheck = None
        self.pair = None
        self.stages = {s: {'status': 'pending', 'attempts': 0} for s in STAGES}


_CTX = {}


def _rec():
    """This thread's recording context (created on first use).

    Keyed by thread id rather than a module global because teamkit's own
    orchestrator can be driven from a non-main thread; per-run state must not
    leak between concurrent runs in one process.
    """
    ident = _thread_ident()
    ctx = _CTX.get(ident)
    if ctx is None:
        ctx = _CTX[ident] = _Ctx()
    return ctx


def _reset_rec(rec):
    """Clear a context so a finished (or refused) run leaves no stale state."""
    rec.recorder = rec.lock_path = rec.run_dir = rec.baseline_ref = None
    rec.baseline = rec.workcheck = rec.pair = None
    rec.stages = {s: {'status': 'pending', 'attempts': 0} for s in STAGES}


def _thread_ident():
    try:
        return os.get_thread_identity()
    except AttributeError:
        import threading
        return threading.get_ident()


def run_slug(workspace):
    """Slug = first 24 safe chars of basename(workspace)."""
    s = re.sub(r'[^A-Za-z0-9._-]', '', os.path.basename(str(workspace).rstrip('/')))
    return s[:24]


def acquire_run_lock(slug, runs_root=RUNS_DIR):
    """O_EXCL lock per run slug holding our pid. Stale lock (dead pid) reclaimed.

    Returns (run_dir, lock_path). Raises FileLockError if a LIVE runner holds
    it — call this before spending anything.
    """
    lock_dir = os.path.join(runs_root, '.locks')
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, '%s.lock' % slug)
    for attempt in (0, 1):
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            holder = None
            try:
                holder = int((open(lock_path).read().strip() or '0'))
            except Exception:
                holder = None
            if holder and holder != os.getpid() and _pid_alive(holder):
                raise FileLockError(
                    f"run slug '{slug}' is locked by live pid {holder} ({lock_path}); "
                    "second runner refused before spending API calls")
            try:            # stale (or unreadable) lock: reclaim it
                os.unlink(lock_path)
            except OSError:
                pass
            continue
        with os.fdopen(fd, 'w') as fh:
            fh.write(str(os.getpid()))
        run_dir = _unique_run_dir(runs_root, slug)
        os.makedirs(run_dir)
        return run_dir, lock_path
    raise FileLockError(f"cannot reclaim stale lock for run slug '{slug}'")


def _unique_run_dir(runs_root, slug):
    """runs/<utc ts>-<slug>; a second run in the same second gets -2, -3 ...

    Two runs must never share a record directory: summary.json would overwrite
    another run's evidence and the trail becomes unreadable.
    """
    ts = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    base = os.path.join(runs_root, '%s-%s' % (ts, slug))
    if not os.path.exists(base):
        return base
    n = 2
    while os.path.exists('%s-%d' % (base, n)):
        n += 1
    return '%s-%d' % (base, n)


def _pid_alive(pid):
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def release_run_lock(rec):
    """Best-effort; recording machinery must never crash a run."""
    try:
        if rec and rec.lock_path and os.path.exists(rec.lock_path):
            os.unlink(rec.lock_path)
    except Exception:
        pass
    try:
        _reset_rec(rec)
    except Exception:
        pass


def start_recording(ws, runs_root=None):
    """Lock + create the run dir for this thread's context.

    Raises FileLockError if a live runner already holds the slug's lock.
    """
    root = runs_root or os.environ.get('TEAMKIT_RUNS_DIR') or RUNS_DIR
    r = _rec()
    _reset_rec(r)
    r.run_dir, r.lock_path = acquire_run_lock(run_slug(ws), root)
    r.recorder = RunRecorder(r.run_dir)
    return r


def sha256_text(text):
    return hashlib.sha256((text or '').encode('utf-8', 'replace')).hexdigest()


class RunRecorder:
    """Writes config/state/summary/retained JSON. Failures inside are swallowed
    where the caller's contract is 'must never crash run_team()'."""

    def __init__(self, run_dir):
        self.run_dir = run_dir
        #: R4b-D6: the recall projection the summary must carry, set by run_team()
        #: from the private `_recall_info` carrier before it writes the record.
        self.recall = None

    def write_json(self, name, obj):
        tmp = os.path.join(self.run_dir, name + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(obj, fh, indent=1, sort_keys=True, default=str)
        os.replace(tmp, os.path.join(self.run_dir, name))

    def config(self, obj):
        self.write_json('config.json', obj)

    def state(self, stages):
        self.write_json('state.json', stages)

    def summary(self, result):
        d = dict(result)
        d['run_dir'] = self.run_dir
        # D6: summary.json carries the same recall attribution config.json does.
        # Added to the RECORD, never to the returned dict, so callers that assert
        # the documented key set keep seeing exactly that set.
        if self.recall:
            d['recall'] = self.recall
        self.write_json('summary.json', d)
        return d

    def retained(self, obj):
        self.write_json('retained.json', obj)


def record_config(rec, cfg):
    """config.json — never raises: a broken recorder must not crash a run."""
    try:
        rec.recorder.config(cfg)
    except Exception:
        pass


def record_recall(rec, recall_info):
    """R4b-D6: hand the recorder this run's recall attribution.

    config.json gets it as part of _record_config(); summary.json is written from
    run_team()'s result dict, which must keep its documented key set, so the
    projection rides on the recorder instead. Set once, as soon as the hub read
    has happened, so even a run that later raises carries its attribution.
    """
    try:
        rec.recorder.recall = _recall_summary(recall_info)
    except Exception:
        pass


def stage_start(rec, name):
    try:
        st = rec.stages[name]
        st['status'] = 'running'
        st['attempts'] = int(st.get('attempts', 0)) + 1
        rec.recorder.state(rec.stages)
    except Exception:
        pass


def stage_end(rec, name, status='done', error=None):
    try:
        st = rec.stages[name]
        st['status'] = status
        if error is not None:
            st['error'] = f'{type(error).__name__}: {str(error)[-300:]}'
        rec.recorder.state(rec.stages)
    except Exception:
        pass


def write_record(rec, result):
    """summary.json (exactly run_team's dict + run_dir). Returns run_dir/None."""
    if rec is None or rec.recorder is None:
        return None
    try:
        return rec.recorder.summary(result)['run_dir']
    except Exception:
        return None


# ------------------------------------------------------------- D3: chokepoint
def _refused(budget_calls):
    """Existing return shape, emptied. Zero side effects: no preflight, no
    network, no run dir, no lock. `outcome` = refused: a chokepoint is not a
    failed gate, so it must never land in failure stats."""
    return {"status": "BUDGET_REFUSED",
            "gate": None, "gate_tail": "", "lessons": None,
            "workdir_diff": "", "recent_commits": "", "orchestrator_final": "",
            "run_dir": None, "pair": None, "outcome": "refused",
            "reason": f"budget_calls={budget_calls} > {BUDGET_MAX_CALLS}; refusing before preflight"}


# ---------------------------------------------------------------- D2: gate v2
def git_workspace(workspace):
    """Resolve the git work-tree that hosts `workspace` (walks up if needed).
    Returns (root, pathspec) or (None, None) when not in a repo."""
    p = subprocess.run(['git', '-C', str(workspace), 'rev-parse', '--show-toplevel'],
                       capture_output=True, text=True)
    if p.returncode == 0 and p.stdout.strip():
        root = p.stdout.strip()
        rel = os.path.relpath(os.path.abspath(workspace), root)
        return root, ('' if rel == '.' else rel)
    return None, None


def _git_out(args, cwd=None):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
        return p.stdout.strip() if p.returncode == 0 else ''
    except Exception:
        return ''


def _git_try(args, cwd=None):
    """(ok, error_text) from a git command — ok is the RETURN CODE, never
    "stderr was empty", which is how a silently refused worktree read as success."""
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    except Exception as exc:
        return False, f'{type(exc).__name__}: {exc}'
    return (p.returncode == 0), ((p.stderr or '').strip() or
                                 f'exit {p.returncode}')


def capture_baseline_ref(workspace):
    """Pristine baseline, captured AT RUN START before the team touches anything.

    Dirty workdir -> `git stash create` commit sha (created but NOT applied, so
    the user's workdir is untouched). Clean -> HEAD sha.

    Baseline means COMMITTED/STASHED state only: uncommitted tracked
    modifications are included via the stash commit, but untracked files are
    NOT. That is deliberate — see README (pair acceptance vs the incumbent).
    """
    root, _ = git_workspace(workspace)
    if not root:
        return None
    status = subprocess.run(['git', '-C', root, 'status', '--porcelain'],
                            capture_output=True, text=True).stdout.strip()
    if status:
        sc = subprocess.run(['git', '-c', 'user.email=teamkit@local',
                             '-c', 'user.name=teamkit', 'stash', 'create'],
                            cwd=root, capture_output=True, text=True).stdout.strip()
        if sc:
            return sc
        # 'create' yields nothing when only untracked files exist -> HEAD
    return _git_out(['git', '-C', root, 'rev-parse', 'HEAD']) or None


def materialize_baseline(baseline_ref, workspace):
    """Pristine copy of `baseline_ref` in a temp dir OUTSIDE runs/.

    Returns (dest, cleanup_fn). `git worktree add --detach` first; if that fails
    (some trees refuse worktrees) fall back to `git archive | tar -x`. Never
    mutates the user workspace except the internal, prunable `.git` worktree
    registration that `git worktree remove --force` + `prune` undo on cleanup.
    """
    root, rel = git_workspace(workspace)
    dest = tempfile.mkdtemp(prefix='teamkit-baseline-',
                            dir=os.environ.get('TEAMKIT_BASE_TMP') or None)
    made_worktree = False

    def cleanup():
        if made_worktree:
            subprocess.run(['git', '-C', root, 'worktree', 'remove', '--force',
                            dest], capture_output=True, text=True)
        else:
            shutil.rmtree(dest, ignore_errors=True)
        if root:                       # prune the internal registration only
            subprocess.run(['git', '-C', root, 'worktree', 'prune'],
                           capture_output=True, text=True)

    err = 'no git work tree'
    if root and os.path.isdir(dest) and not os.listdir(dest):
        ok, err = _git_try(['git', '-C', root, 'worktree', 'add', '--detach',
                            dest, baseline_ref], cwd=root)
        if ok:
            made_worktree = True
        else:
            # Fallback: `git archive | tar -x`. worktree add is refused on some
            # trees, so the baseline still gets materialized, just untracked.
            shutil.rmtree(dest, ignore_errors=True)
            dest = tempfile.mkdtemp(prefix='teamkit-baseline-',
                                    dir=os.environ.get('TEAMKIT_BASE_TMP') or None)
            arc = subprocess.run(['git', '-C', root, 'archive', '--format=tar',
                                  baseline_ref], capture_output=True)
            if arc.returncode != 0:
                cleanup()
                raise RuntimeError(
                    'baseline materialization failed: worktree=' + str(err)[-200:]
                    + ' archive=' + arc.stderr.decode('utf-8', 'replace')[-200:])
            try:
                tar = subprocess.run(['tar', '-x', '-C', dest],
                                     input=arc.stdout)
            except FileNotFoundError as exc:      # no tar binary
                cleanup()
                raise RuntimeError('baseline materialization failed: ' + str(exc))
            if tar.returncode != 0:
                cleanup()
                raise RuntimeError('baseline materialization failed: tar exit '
                                   + str(tar.returncode))
    if rel:
        return os.path.join(dest, rel), cleanup
    return dest, cleanup


def run_gate(cmd, cwd):
    """Run one gate command, timeout 900. -> {ok, rc, tail, error}."""
    try:
        p = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True,
                           text=True, timeout=GATE_TIMEOUT)
        return {"ok": p.returncode == 0, "rc": p.returncode, "ran": True,
                "tail": ((p.stdout or '')[-500:] + (p.stderr or '')[-300:]),
                "error": None}
    except Exception as exc:
        return {"ok": False, "rc": None, "ran": False, "tail": str(exc)[-500:],
                "error": f'{type(exc).__name__}: {str(exc)[-300:]}'}


def legacy_gate(contract, workspace):
    """Pre-R1 gate: `gate` in the workspace once, exit 0 -> GATE_PASS.

    The single implementation of the legacy path, shared by the normal flow and
    the (unreachable-by-design) no-recorder fallback, so behaviour cannot drift.
    """
    r = run_gate(contract['gate'], workspace)
    return ('GATE_PASS' if r['ok'] else 'GATE_FAIL'), r['ok'], r['tail'], None


def evaluate_gate(rec, contract, workspace):
    """Gate v1 (legacy, component only) or v2 (component + pair vs incumbent).

    Exact statuses: g_new fail -> GATE_FAIL (incumbent retained); pass+pass ->
    GATE_PASS_TIE; pass+fail -> GATE_PASS_NEW. With incumbent_gate present the
    legacy string GATE_PASS is never emitted.
    """
    if rec is None:                     # recording unavailable: legacy path only
        if not (contract and contract.get('gate')):
            return 'RUN_DONE_NO_GATE', None, '', None
        return legacy_gate(contract, workspace)
    gate_cmd = (contract or {}).get('gate')
    inc_cmd = (contract or {}).get('incumbent_gate')
    stage_start(rec, 'gate')
    if not gate_cmd:
        stage_end(rec, 'gate', 'skipped')
        return 'RUN_DONE_NO_GATE', None, '', None
    r_new = run_gate(gate_cmd, workspace)
    pair = None
    if not inc_cmd:
        stage_end(rec, 'gate', 'done' if r_new['ok'] else 'fail',
                  None if r_new['ok'] else RuntimeError(r_new['tail'][-300:]))
        status = 'GATE_PASS' if r_new['ok'] else 'GATE_FAIL'
        return status, r_new['ok'], r_new['tail'], None
    baseline_ref = (rec.baseline_ref or capture_baseline_ref(workspace))
    head_ref = _git_out(['git', '-C', workspace, 'rev-parse', 'HEAD'])
    r_base = {'ok': False, 'rc': None, 'tail': '', 'ran': False,
              'error': 'no baseline ref: workspace is not a git work tree'}
    cleanup = None
    try:
        if baseline_ref:
            bdir, cleanup = materialize_baseline(baseline_ref, workspace)
            r_base = run_gate(inc_cmd, bdir)
            r_base['ran'] = True
    except Exception as exc:
        r_base = {'ok': False, 'rc': None, 'tail': str(exc)[-500:], 'ran': False,
                  'error': f'{type(exc).__name__}: {str(exc)[-300:]}'}
    finally:
        if cleanup:
            try:
                cleanup()
            except Exception:
                pass
    accepted = bool(r_new['ok'])
    if not r_new['ok']:
        status = 'GATE_FAIL'                       # incumbent retained
    elif r_base['ok']:
        status = 'GATE_PASS_TIE'                   # new matches the baseline
    elif r_base['ran']:
        status = 'GATE_PASS_NEW'                   # improvement, retained
    else:
        # A pair check that could not run proves nothing about the incumbent, so
        # no improvement is claimed: TIE, with the reason recorded in g_base.error.
        status = 'GATE_PASS_TIE'
    stage_end(rec, 'gate', 'done' if accepted else 'fail',
              None if accepted else RuntimeError(r_new['tail'][-300:]))
    g_new = {'ok': r_new['ok'], 'rc': r_new['rc'], 'cmd': gate_cmd,
             'error': r_new['error']}
    g_base = {'ok': r_base['ok'], 'rc': r_base['rc'], 'cmd': inc_cmd,
              'error': r_base['error'], 'ran': bool(r_base.get('ran', True))}
    pair = {'g_new': g_new, 'g_base': g_base, 'baseline_ref': baseline_ref}
    try:
        rec.recorder.retained({'accepted': accepted, 'baseline_ref': baseline_ref,
                               'head_ref': head_ref or None,
                               'g_new': g_new, 'g_base': g_base})
    except Exception:
        pass
    return status, accepted, (r_new['tail'] + '\n' + r_base['tail'])[-800:], pair


# ---------------------------------------------------------- D4: outcome split
# "Did the system fail" and "did the system do nothing" are different diseases:
# a failed gate needs a better brief, a zero-work stall needs a lane fix.
OUTCOMES = ('success', 'partial_work', 'abstained', 'refused', 'tie_kept')
_OUTCOME_SUCCESS = ('GATE_PASS', 'GATE_PASS_NEW')
_OUTCOME_WORKED = ('GATE_FAIL', 'RUN_DONE_NO_GATE', 'ERROR')
_OUTCOME_REFUSED = ('BUDGET_REFUSED', 'DRY_PLAN')


def capture_workspace_state(workspace):
    """READ-ONLY run-start (and run-finish) fingerprint of a workspace.

    Keys: root (git toplevel, or None when not a work tree), ws (the path it was
    captured from), head (HEAD sha, or '' for an unborn repo), tracked (the full
    `git diff HEAD` text), porcelain (the full `git status --porcelain -uall`
    text) and sha256 digests of both.

    Reuses the `git status --porcelain` capture pattern from
    capture_baseline_ref(), but never runs `git stash create` — it leaves no
    dangling object in the user's repo — so it is cheap and safe on EVERY path,
    including runs with no incumbent_gate. Both digests are repo-wide, because
    that is what the team can reach: a sub-agent holds a shell, so scoping the
    diff to the workspace subdirectory would credit the run with edits it never
    made (or hide edits it made next door).
    """
    root, _rel = git_workspace(workspace)
    if not root:
        return {'root': None, 'ws': str(workspace), 'head': '', 'tracked': '',
                'porcelain': '', 'tracked_sha': '', 'porcelain_sha': ''}
    porcelain = _git_out(['git', '-C', str(root), 'status', '--porcelain', '-uall'])
    tracked = _git_out(['git', '-C', str(root), 'diff', '--no-color', 'HEAD'])
    return {'root': root, 'ws': str(workspace),
            'head': _git_out(['git', '-C', str(root), 'rev-parse', 'HEAD']),
            'tracked': tracked, 'tracked_sha': sha256_text(tracked),
            'porcelain': porcelain, 'porcelain_sha': sha256_text(porcelain)}


def work_evidence(base, after=None):
    """What the run actually produced, between two fingerprints.

    {'commits': int|None, 'tracked_changed': bool, 'dirt_changed': bool,
     'new_dirt': [porcelain lines], 'measurable': bool}. commits is None (and
    measurable False) when the workspace is not a git tree or had no HEAD at
    run start: 'we cannot see' must never be reported as 'the team did nothing'.
    """
    base = base or {}
    after = after if after is not None else capture_workspace_state(
        base.get('root') or '.')
    known = set((base.get('porcelain') or '').splitlines())
    return {'commits': _commits_since(base, after),
            'tracked_changed': (base.get('tracked_sha') or '') !=
                               (after.get('tracked_sha') or ''),
            'dirt_changed': (base.get('porcelain_sha') or '') !=
                            (after.get('porcelain_sha') or ''),
            'new_dirt': [l for l in (after.get('porcelain') or '').splitlines()
                         if l and l not in known],
            'measurable': _commits_since(base, after) is not None}


def _commits_since(base, after):
    """`git rev-list --count <base head>..<after head>`; 0 when HEAD is the same;
    None when either side is unknown (no git tree, unborn repo)."""
    b, a = (base or {}).get('head') or '', (after or {}).get('head') or ''
    if not b or not a:
        return None
    if b == a:
        return 0
    root = (after or {}).get('root') or (base or {}).get('root')
    n = _git_out(['git', '-C', str(root), 'rev-list', '--count',
                  '%s..%s' % (b, a)])
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


def _has_work(ev):
    """Evidence says the run left something behind."""
    if not ev or not ev.get('measurable'):
        return True            # cannot prove zero work -> never claim abstention
    return bool(ev.get('commits')) or ev.get('tracked_changed') or \
        ev.get('dirt_changed')


def produce_work(base, after=None):
    """True when the run left evidence: new commits, a changed tracked diff, or
    dirt that was not present at run start."""
    return _has_work(work_evidence(base, after))


def classify_outcome(status, base=None, after=None, ev=None):
    """One of OUTCOMES for a finished (or refused, or crashed) run.

    base/after are capture_workspace_state() dicts and `ev` is a precomputed
    work_evidence(); `after` defaults to a fresh capture of base's root.
    GATE_PASS / GATE_PASS_NEW -> success; GATE_PASS_TIE -> tie_kept (retained,
    not proven better); BUDGET_REFUSED / DRY_PLAN -> refused (zero spend, kept
    out of failure stats); GATE_FAIL / RUN_DONE_NO_GATE / ERROR -> partial_work
    when there is work evidence, abstained when the run touched nothing.
    Never raises; an unlisted status is treated as a failure, not a stall.
    """
    try:
        if status in _OUTCOME_SUCCESS:
            return 'success'
        if status == 'GATE_PASS_TIE':
            return 'tie_kept'
        if status in _OUTCOME_REFUSED:
            return 'refused'
        if status in _OUTCOME_WORKED:
            ev = ev if ev is not None else work_evidence(base, after)
            return 'partial_work' if _has_work(ev) else 'abstained'
        return 'partial_work'
    except Exception:
        return 'partial_work'


def _spend_started(rec):
    """Did this run reach the `team_run` stage, where conv.run() can spend?

    D4: an ERROR raised earlier — preflight, lane check, agent registration —
    spent zero calls, so it is a refusal, not a failure of the team.
    """
    try:
        return int(rec.stages.get('team_run', {}).get('attempts', 0)) > 0
    except Exception:
        return True


def _outcome_for(rec, status, ev=None):
    """classify_outcome() wired to this run's run-start fingerprint."""
    if status == 'ERROR' and not _spend_started(rec):
        return 'refused'
    return classify_outcome(status, getattr(rec, 'baseline', None), ev=ev)


def _work_evidence_for(rec):
    """Evidence between run start and the moment the team finished.

    Taken BEFORE the gate on purpose (see _capture_workcheck): a gate that runs
    a test suite creates __pycache__/.pytest_cache dirt of its own, which must
    not be mistaken for work the team produced — that would relabel a stall as
    partial_work, the exact statistic this split exists to keep clean.
    """
    try:
        return work_evidence(rec.baseline, getattr(rec, 'workcheck', None))
    except Exception:
        return None


def _capture_workcheck(rec, workspace):
    """Fingerprint the workspace the instant the team stops (or crashes)."""
    try:
        rec.workcheck = capture_workspace_state(os.path.abspath(workspace or '.'))
    except Exception:
        rec.workcheck = None


def _emit_plan(run_dir, plan):
    print('TEAMKIT DRY_PLAN')
    for k, v in plan.items():
        print(f'  {k}: {v if not isinstance(v, dict) else json.dumps(v, sort_keys=True)}')
    print(f'  run_dir: {run_dir}')
    sys.stdout.flush()


def run_team(brief=None, spec=None, workspace='.', budget_calls=80, contract=None,
             model="openai/qwen3.8-flash", base_url=None, roles=None,
             lessons=None, waive_lessons=None, max_empty_responses=3,
             on_run=None):
    """Run the team, then grade it — with a run record and a budget guard.

    Pre-existing behaviour is unchanged; wrapped around it: D3 chokepoints first
    (BUDGET_REFUSED / DRY_PLAN, both before preflight), then the per-slug lock,
    then the run itself under stage recording. Returns the legacy keys plus
    `run_dir`, `pair`, `outcome` and `work_evidence` (D4). `on_run` is a test
    seam, called where conv.run() edits.
    """
    # ---- D3 chokepoint: refuse / plan BEFORE preflight, before any spend.
    try:
        _b = int(budget_calls)
    except (TypeError, ValueError):
        _b = BUDGET_MAX_CALLS + 1
    if _b > BUDGET_MAX_CALLS:
        return _refused(budget_calls)
    rec = _rec()
    if os.environ.get('TEAMKIT_DRY_RUN') == '1':
        _ws = os.path.abspath(workspace or '.')
        _m, _u = _lane_check(model, base_url or
                             (TP_COMPAT if _m0(model) else TP_ANTH))
        plan = {"workspace": _ws, "model": _m, "base_url": _u,
                "budget_calls": _b, "roles": roles, "stages": list(STAGES),
                "gate": (contract or {}).get('gate'),
                "incumbent_gate": (contract or {}).get('incumbent_gate')}
        run_dir, cfg = None, None
        try:
            rec = start_recording(_ws)
            cfg = _record_config(_ws, _m, _u, budget_calls, contract, roles,
                                 brief, spec, rec.run_dir)
            record_config(rec, cfg)
            rec.recorder.state({s: {'status': 'skipped', 'attempts': 0}
                                for s in STAGES})
            run_dir = rec.run_dir
        except Exception:
            run_dir = None
        release_run_lock(rec)
        plan['config'] = cfg
        _emit_plan(run_dir, plan)
        return {"status": "DRY_PLAN", "gate": None, "gate_tail": "",
                "lessons": None, "workdir_diff": "", "recent_commits": "",
                "orchestrator_final": "", "run_dir": run_dir, "pair": None,
                "outcome": "refused",
                "plan": {"workspace": _ws, "model": _m, "base_url": _u,
                         "budget_calls": _b, "roles": roles,
                         "stages": list(STAGES),
                         "gate": (contract or {}).get('gate'),
                         "incumbent_gate": (contract or {}).get('incumbent_gate')}}

    # ---- D1: one lock per run slug, taken BEFORE any API call can be spent.
    rec = start_recording(os.path.abspath(workspace or '.'))
    # ---- D4: read-only run-start fingerprint, captured on EVERY path (with or
    # without incumbent_gate) before the team touches anything. It is what makes
    # "no new commits AND no diff change" measurable without crediting the run
    # for dirt the owner already had: digests are compared, not presence.
    try:
        rec.baseline = capture_workspace_state(os.path.abspath(workspace or '.'))
    except Exception:
        rec.baseline = None
    # ---- D2: baseline captured at run start, before the team touches anything.
    # Only when v2 is in play: `stash create` writes a dangling commit object
    # into the user's repo, and a legacy single-gate run has no use for it.
    if (contract or {}).get('incumbent_gate'):
        try:
            rec.baseline_ref = capture_baseline_ref(os.path.abspath(workspace or '.'))
        except Exception:
            rec.baseline_ref = None
    try:
        # config.json up front, so the record survives even a stage that raises.
        # _run_team_inner rewrites it once lane_check resolved model/base_url.
        record_config(rec, _record_config(
            os.path.abspath(workspace or '.'), model,
            base_url or (TP_COMPAT if _m0(model) else TP_ANTH),
            budget_calls, contract, roles, brief, spec, rec.run_dir))
        result = _run_team_inner(rec, brief, spec, workspace, budget_calls,
                                 contract, model, base_url, roles, lessons,
                                 waive_lessons, max_empty_responses, on_run=on_run)
        # R4-D4: recall attribution already merged into config.json by
        # _run_team_inner; drop the private carrier key so the returned dict and
        # summary.json keep exactly the documented key set.
        result.pop('_recall_info', None)
        rec.pair = result.get('pair')
        # D4: outcome computed AFTER gating, from the run-start fingerprint vs
        # the state captured when the team stopped. Existing keys keep their
        # meaning; only outcome and work_evidence are added.
        ev = _work_evidence_for(rec)
        result['outcome'] = _outcome_for(rec, result.get('status'), ev)
        result['work_evidence'] = ev
        # existing keys stay byte-compatible; ONLY run_dir is added (None if the
        # recording itself failed)
        result['run_dir'] = write_record(rec, result)
        return result
    except Exception as exc:
        status = 'ERROR'
        ev = _work_evidence_for(rec)
        write_record(rec, {"status": status,
                           "error": f'{type(exc).__name__}: {str(exc)[-300:]}',
                           "gate": None, "gate_tail": "", "lessons": None,
                           "workdir_diff": "", "recent_commits": "",
                           "orchestrator_final": "", "pair": None,
                           "outcome": _outcome_for(rec, status, ev),
                           "work_evidence": ev})
        raise
    finally:
        release_run_lock(rec)


def _m0(model):
    return str(model or '').startswith('openai/')


def _recall_summary(recall):
    """R4b-D6: the config.json `recall` key, from a _recall_info() dict.

    {recall_status: fired|empty|unavailable|error, hits: [{name|sha8, chars}]}.
    `unavailable` (no key / no bridge) is recorded explicitly rather than being
    absent: "memory said nothing" and "memory was never reachable" have to stay
    distinguishable in the record, which is the entire point of the status.
    Keys of the info dict are the contract (RECALL_STATUSES), so this is a
    projection, not a re-derivation.
    """
    if not recall:
        return None
    return {'recall_status': recall.get('recall_status'),
            'hits': [{'name': h['name'], 'chars': h.get('chars', 0)}
                     if 'name' in h else
                     {'sha8': h.get('sha8', ''), 'chars': h.get('chars', 0)}
                     for h in recall.get('recall_hits') or []]}


def _record_config(ws, model, base_url, budget_calls, contract, roles, brief,
                   spec, run_dir=None, recall=None, agents=None,
                   task_start_ts=None):
    """config.json payload: resolved inputs + sha256 of the brief text (never
    the text itself) + the contract verbatim + R4-D4 recall attribution.

    `recall` is the _recall_info() dict. Flat `recall_status`/`recall_hits` are
    kept for every reader that landed before R4b; R4b-D6 adds the nested
    `recall` summary, which is the shape the acceptance contract names.
    `agents` is R4b-D7's registration summary (registered names, normalized
    model values, per-role caps, agent warnings); recorded only when a refresh
    actually ran. `task_start_ts` (R4b-D8) is the value exported to
    $TASK_START_TS for every child process, so the record and the tester's
    ledger window can be reconciled.
    """
    msg_text = (brief if brief else spec) or ''
    cfg = {"run_dir": run_dir, "workspace": ws, "model": model,
           "base_url": base_url, "budget_calls": budget_calls,
           "contract": contract, "roles": roles,
           "brief_sha256": sha256_text(msg_text)}
    if task_start_ts is not None:
        cfg['task_start_ts'] = task_start_ts
    if recall:
        cfg['recall_status'] = recall.get('recall_status')
        cfg['recall_hits'] = recall.get('recall_hits') or []
        cfg['recall'] = _recall_summary(recall)
        if recall.get('recall_error'):
            cfg['recall_error'] = recall['recall_error']
    if agents:
        cfg['agents'] = agents
    return cfg


def _run_team_inner(rec, brief, spec, workspace, budget_calls, contract, model,
                    base_url, roles, lessons, waive_lessons,
                    max_empty_responses, on_run=None):
    """The original run_team() body, stage-recorded by the run_team() wrapper.

    Every pre-existing step stays in order (preflight -> lane check -> register
    agents -> send_message/run -> gate -> lessons -> git diff/log -> return);
    only stage bookkeeping and the gate evaluation are wrapped. `on_run` exists
    for tests: it is called at the point conv.run() would have mutated the
    workspace, so "baseline captured at run start" can be proven deterministically.
    """
    stage_start(rec, 'preflight')
    try:
        # R4b-D8: team/roles/tester.md reads $TASK_START_TS to bound its ledger
        # delta to THIS run; nothing set it, so the window was always "the whole
        # session". Exported before any subprocess exists (gate, agent terminal,
        # the tester one-liner) and recorded so the record says what the children
        # were given.
        task_start_ts = int(time.time())
        os.environ['TASK_START_TS'] = str(task_start_ts)
        # R4b-D7b: the run budget bounds whatever a role file declares.
        iter_cap = max(1, int(budget_calls) * 3)
        # R4b-D7a: BOTH modes force-refresh. Orchestrator mode starts from the
        # AGENTS_DIR roles preflight() parsed and adds this workspace's own file
        # agents; builder mode starts from the workspace files alone (it has no
        # AGENTS_DIR roles, since nothing delegates). refresh_workspace_agents
        # pops each name out of `_agent_factories` and re-registers it from the
        # CURRENT disk file, which is the only way an already-registered name
        # picks up an edit (the SDK uses register_agent_if_absent: absent-only).
        # `defs` below stays the AGENTS_DIR list so the orchestrator's
        # registration loop and the general-purpose alias behave exactly before.
        if roles == 'none':
            defs = []
            refresh, agent_warns = refresh_workspace_agents(workspace,
                                                            cap=iter_cap)
        else:
            defs = preflight()
            refresh, agent_warns = refresh_workspace_agents(
                workspace, defs=list(defs) + _workspace_defs(workspace),
                cap=iter_cap)
        agents_summary = {"registered": refresh['names'],
                          "normalized": refresh['normalized'],
                          "caps": refresh['caps'],
                          "builder_mode": roles == 'none',
                          "iter_cap": iter_cap}
        if agent_warns:
            agents_summary["warnings"] = agent_warns
    except Exception as exc:
        stage_end(rec, 'preflight', 'fail', exc)
        raise
    stage_end(rec, 'preflight', 'done')
    # `defs` deliberately stays the AGENTS_DIR list: refresh_workspace_agents()
    # has already registered those (normalized, cap-clamped) objects plus the
    # workspace's own file agents, and the loop below repeats the AGENTS_DIR
    # registration exactly as it did before, so the orchestrator's alias handling
    # and the run's recorded `agents` summary describe the same set of names.
    from openhands.sdk import LLM, Agent, Conversation
    from openhands.tools.preset.default import get_default_tools
    from openhands.sdk.security.confirmation_policy import NeverConfirm
    from openhands.sdk.subagent import register_agent_if_absent, agent_definition_to_factory

    base_url = base_url or (TP_COMPAT if model.startswith('openai/') else TP_ANTH)
    model, base_url = _lane_check(model, base_url)
    # TDAI read path, run BEFORE the config record is written so attribution
    # lands in config.json (R4-D4). One hub read per run: the same dict feeds both
    # the record and the PRIOR LEARNINGS block appended to the message below.
    recall_info = _recall_info(_tdai(), os.path.basename(str(workspace).rstrip('/'))
                               + ' ' + (brief or spec or '')[:400])
    record_recall(rec, recall_info)      # so summary.json carries it too (D6)
    record_config(rec, _record_config(os.path.abspath(workspace or '.'), model,
                                      base_url, budget_calls, contract, roles,
                                      brief, spec, rec.run_dir,
                                      recall=recall_info, agents=agents_summary,
                                      task_start_ts=task_start_ts))
    llm = LLM(model=model, api_key=_key(), base_url=base_url)
    # NOTE (R4b-D7): the registration below is NOT the force-refresh. That
    # already happened inside refresh_workspace_agents() at the top of this
    # function, for BOTH modes, with the model normalization and the cap clamp
    # applied to the in-memory definitions. What remains here keeps the
    # orchestrator's explicit registration contract for the AGENTS_DIR roles
    # (pop + register + the general-purpose alias) using those same refreshed
    # definition objects, so the registry can never hold a pre-normalization or
    # pre-clamp copy of a role.
    from openhands.sdk.subagent.registry import _agent_factories, _registry_lock
    for d in defs:
        # force-refresh: drop any stale registration from a previous run so the
        # CURRENT md file on disk is what actually executes (silent-drift trap)
        with _registry_lock:
            _agent_factories.pop(d.name, None)
        # description is the DEFINITION object, never a bare string: a string
        # becomes a metadata-empty AgentDefinition inside the SDK, and that is
        # where the Task manager reads max_iteration_per_run and
        # permission_mode from (R4b-D7b).
        register_agent_if_absent(d.name, agent_definition_to_factory(d, workspace), d)
    # Task tool defaults subagent_type='general-purpose'; without it registered,
    # any such call dies with 'Unknown agent'. Map it to fixer as a safe alias.
    if 'general-purpose' not in _agent_factories and 'fixer' in _agent_factories:
        gf = _agent_factories['fixer']
        register_agent_if_absent('general-purpose', gf.factory_func, gf.definition)

    # JSON-only handoff contract between orchestrator and its sub-agents
    if roles == 'none':
        sys_prompt = (
            "You are a BUILDER agent: implement the brief yourself in your workspace, run the gate "
            f"command yourself, iterate until it passes. Hard budget: {budget_calls} API calls. "
            "Final message must be exactly one JSON object: "
            "{\"status\":\"complete|partial\",\"files\":[...],\"evidence\":\"gate cmd + exit code\"}."
        )
        tools = get_default_tools(enable_browser=False, enable_sub_agents=False)
    else:
        sys_prompt = (
            "You are the ORCHESTRATOR (never write code; delegate via Task tool to 'architect' then 'fixer'). "
            "Sub-agent handoffs: give each a self-contained task id, file scope, acceptance criteria; demand back "
            "JSON only: {\"task\":\"...\",\"status\":\"done|blocked\",\"files\":[...],\"evidence\":\"file:line or cmd:exit\"}. "
            f"Hard budget: {budget_calls} API calls total this run. When all contract items are implemented, STOP. "
            "Final message must be exactly one JSON object: "
            "{\"status\":\"complete|partial\",\"tasks_done\":[...],\"notes\":\"<=5 bullets\"}."
        )
        tools = get_default_tools(enable_browser=False, enable_sub_agents=True)
    agent = Agent(llm=llm, tools=tools, system_prompt=sys_prompt)
    # R4b-D7b: `iter_cap` is exactly the old budget_calls * 3, hoisted into the
    # preflight block because the same number must bound every per-role
    # definition cap; min(def, iter_cap) lands on the definition objects above
    # and this Conversation is the parent they inherit from.
    conv = Conversation(agent=agent, workspace=workspace, max_iteration_per_run=iter_cap,
                        stuck_detection_thresholds={"action_observation": 6, "action_error": 6,
                                                    "monologue": 8, "alternating_pattern": 6})
    if roles == 'none':
        conv.set_confirmation_policy(NeverConfirm())  # v1.54: policy is setter-only, never ctor kwarg
    msg = (brief if brief else spec) or ""
    if contract:
        msg += "\n\nDELIVERABLE CONTRACT (verify each): " + json.dumps(contract, ensure_ascii=False)
    # TDAI read path: shared team memory recall appended when a key is present.
    # The hub call already happened above (once per run), and its attribution is
    # in config.json; R4-D4 matters because this text is appended AFTER brief
    # hashing, so the record alone could not otherwise say what memory added.
    msg += _recall_block('', recall_info)[0]
    conv.send_message(msg)
    # iteration cap + tightened stuck thresholds are set at Conversation ctor
    # above; run() itself takes no args in v1.54
    stage_start(rec, 'team_run')
    try:
        # test seam: on_run fires where the team is about to edit the workspace,
        # i.e. still inside the run and before the gate is graded
        if on_run:
            on_run(workspace)
        conv.run()
    except Exception as exc:
        _capture_workcheck(rec, workspace)
        stage_end(rec, 'team_run', 'fail', exc)
        raise
    _capture_workcheck(rec, workspace)
    stage_end(rec, 'team_run', 'done')

    # deterministic grading: component check (+ pair check vs incumbent) and git
    # state — NOT orchestrator prose
    status, gate_ok, gate_out, pair = evaluate_gate(rec, contract, workspace)

    # lessons gate (owner rule 2026-10-11): every run must end with seeded
    # learnings or an explicit waiver.
    # R4-D3 seed policy: a lesson from a run that abstained, failed, or was
    # refused teaches the next run a mistake, so auto-seeding is allowed only
    # for outcome in SEEDABLE_OUTCOMES. Anything else either carries an
    # explicit `lessons=[]` waiver or records SKIPPED_NOT_RETAINED WITHOUT
    # calling the hub. And a batch where fewer notes landed than were handed
    # over is `partial`, never a silently-done stage.
    # A keyless or hub-down bridge is the documented no-op path (hard
    # constraint: behaves exactly as today), so `unavailable` is NOT partial.
    outcome = _outcome_for(rec, status, _work_evidence_for(rec))
    seedable = outcome in SEEDABLE_OUTCOMES
    explicit_waiver = isinstance(lessons, list) and not lessons
    stage_start(rec, 'lessons')
    lesson_status = 'WAIVED: ' + waive_lessons if waive_lessons else None
    if lesson_status is None and explicit_waiver:
        lesson_status = 'WAIVED: lessons=[] (explicit)'
    lesson_err = None
    lesson_partial = False
    try:
        if lessons:
            if seedable:
                res = learn_out(workspace, lessons)
                lesson_status = json.dumps(res)
                landed = int(res.get('seeded', 0) or 0)
                lesson_partial = (landed < len(lessons)
                                  and res.get('tdai') != 'unavailable')
            else:
                lesson_status = f'SKIPPED_NOT_RETAINED: outcome={outcome}'
                lesson_partial = True
        elif not (waive_lessons or explicit_waiver):
            lesson_status = 'LESSONS_REQUIRED: pass lessons=[...] or waive_lessons="reason"'
    except Exception as exc:
        lesson_err = exc
    stage_end(rec, 'lessons',
              'fail' if lesson_err else ('partial' if lesson_partial else 'done'),
              lesson_err)

    diff = subprocess.run(['git', '-C', workspace, 'diff', '--stat'], capture_output=True, text=True)
    committed = subprocess.run(['git', '-C', workspace, 'log', '--oneline', '-5'], capture_output=True, text=True)
    return {
        "status": status,
        "gate": contract.get('gate') if contract else None,
        "gate_tail": gate_out,
        "lessons": lesson_status,
        "workdir_diff": diff.stdout[-1200:],
        "recent_commits": committed.stdout,
        "orchestrator_final": (getattr(conv, 'state', None) and str(conv.state)[-400:]) or '',
        "pair": pair,
        # R4-D4: handed to run_team(), which merges it into config.json and pops
        # so the returned dict keeps its documented key set.
        "_recall_info": recall_info,
    }


if __name__ == '__main__':
    print(json.dumps(preflight()[0].name), "preflight ok")
