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

Three runner disciplines (R1, ported from Gen-Verse/RSI-Anything hygiene ideas):
  D1 RunRecord  every run persists runs/<utc>-<slug>/{config,state,summary}.json,
                guarded by one O_EXCL lock per run slug (FileLockError, pre-spend).
  D2 Gate v2    contract['incumbent_gate'] turns the single gate into a component
                check (workspace) + a pair check against a pristine baseline
                materialized outside runs/ -> GATE_PASS_NEW / GATE_PASS_TIE /
                GATE_FAIL, and retained.json says what was kept.
  D3 Chokepoint budget_calls > 120 -> BUDGET_REFUSED with zero side effects;
                TEAMKIT_DRY_RUN=1 -> DRY_PLAN, record + stdout plan, zero API calls.
Stdlib only, and openhands stays a lazy import so unit tests run on bare python3.
"""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile, time

CFG = '/home/hermes/.hermes/config.yaml'
TP_ANTH = 'https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic'
TP_COMPAT = 'https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1'
AGENTS_DIR = '/home/hermes/.hermes/cache/scratch/oh_team2/agents'
TDAI_BRIDGE = '/home/hermes/.hermes/scripts/tdai-bridge.py'

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
    runs proceed exactly as before (zero regression). Never raises."""
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


def _recall_block(query):
    tb = _tdai()
    if not tb:
        return ''
    hits = tb.recall(query)
    if not hits:
        return ''
    return ("\n\nPRIOR LEARNINGS (shared team memory; treat as hints, verify against repo):\n"
            + "\n---\n".join(hits)) + "\n"


def learn_out(workspace, notes):
    """Post-run distillation into the shared hub. notes: list[str] of durable lessons."""
    tb = _tdai()
    if not tb:
        return {'tdai': 'unavailable'}
    ok = [tb.seed_skill(f'teamkit-lesson-{i}', n) for i, n in enumerate(notes)]
    return {'tdai_seeded': sum(bool(x) for x in ok), 'of': len(notes)}



def _key():
    m = re.search(r"^  api_key: (.*)$", open(CFG).read(), re.M)
    return m.group(1).strip().strip("'").strip('"')


def preflight():
    """Skill rule: verify role defs parse with registered tools BEFORE spending calls."""
    os.environ['OPENHANDS_SUPPRESS_BANNER'] = '1'
    from pathlib import Path
    from openhands.sdk.subagent import load_agents_from_dir
    from openhands.tools.preset.default import get_default_tools
    flat = set()
    for ts in get_default_tools(enable_browser=False, enable_sub_agents=True):
        for t in getattr(ts, 'tools', [ts]):
            flat.add(getattr(t, 'name', str(t)))
    defs = load_agents_from_dir(Path(AGENTS_DIR))
    for d in defs:
        tl = list(d.tools)
        assert tl, f"PREFLIGHT FAIL {d.name}: zero tools (frontmatter?)"
        miss = [x for x in tl if x not in flat]
        assert not miss, f"PREFLIGHT FAIL {d.name}: unregistered {miss}"
        m = getattr(d, 'model', None)
        if isinstance(m, str):
            assert m.strip() in ('', 'inherit'), f"PREFLIGHT FAIL {d.name}: model '{m}' is treated as an LLM profile name and will crash every Task call (use 'inherit')"
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
    """Per-run recording context: lock + recorder + stage ledger + baseline ref."""

    def __init__(self):
        self.recorder = None
        self.lock_path = None
        self.run_dir = None
        self.baseline_ref = None
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
    rec.recorder = rec.lock_path = rec.run_dir = rec.baseline_ref = rec.pair = None
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
    network, no run dir, no lock."""
    return {"status": "BUDGET_REFUSED",
            "gate": None, "gate_tail": "", "lessons": None,
            "workdir_diff": "", "recent_commits": "", "orchestrator_final": "",
            "run_dir": None, "pair": None,
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
    `run_dir` and `pair`. `on_run` is a test seam, called where conv.run() edits.
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
                "plan": {"workspace": _ws, "model": _m, "base_url": _u,
                         "budget_calls": _b, "roles": roles,
                         "stages": list(STAGES),
                         "gate": (contract or {}).get('gate'),
                         "incumbent_gate": (contract or {}).get('incumbent_gate')}}

    # ---- D1: one lock per run slug, taken BEFORE any API call can be spent.
    rec = start_recording(os.path.abspath(workspace or '.'))
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
        rec.pair = result.get('pair')
        # existing keys stay byte-compatible; ONLY run_dir is added (None if the
        # recording itself failed)
        result['run_dir'] = write_record(rec, result)
        return result
    except Exception as exc:
        write_record(rec, {"status": "ERROR",
                           "error": f'{type(exc).__name__}: {str(exc)[-300:]}',
                           "gate": None, "gate_tail": "", "lessons": None,
                           "workdir_diff": "", "recent_commits": "",
                           "orchestrator_final": "", "pair": None})
        raise
    finally:
        release_run_lock(rec)


def _m0(model):
    return str(model or '').startswith('openai/')


def _record_config(ws, model, base_url, budget_calls, contract, roles, brief,
                   spec, run_dir=None):
    """config.json payload: resolved inputs + sha256 of the brief text (never
    the text itself) + the contract verbatim."""
    msg_text = (brief if brief else spec) or ''
    return {"run_dir": run_dir, "workspace": ws, "model": model,
            "base_url": base_url, "budget_calls": budget_calls,
            "contract": contract, "roles": roles,
            "brief_sha256": sha256_text(msg_text)}


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
        defs = [] if roles == 'none' else preflight()
    except Exception as exc:
        stage_end(rec, 'preflight', 'fail', exc)
        raise
    stage_end(rec, 'preflight', 'done')
    from openhands.sdk import LLM, Agent, Conversation
    from openhands.tools.preset.default import get_default_tools
    from openhands.sdk.security.confirmation_policy import NeverConfirm
    from openhands.sdk.subagent import register_agent_if_absent, agent_definition_to_factory

    base_url = base_url or (TP_COMPAT if model.startswith('openai/') else TP_ANTH)
    model, base_url = _lane_check(model, base_url)
    record_config(rec, _record_config(os.path.abspath(workspace or '.'), model,
                                      base_url, budget_calls, contract, roles,
                                      brief, spec, rec.run_dir))
    llm = LLM(model=model, api_key=_key(), base_url=base_url)
    from openhands.sdk.subagent.registry import _agent_factories, _registry_lock
    for d in defs:
        # force-refresh: drop any stale registration from a previous run so the
        # CURRENT md file on disk is what actually executes (silent-drift trap)
        with _registry_lock:
            _agent_factories.pop(d.name, None)
        register_agent_if_absent(d.name, agent_definition_to_factory(d, workspace), d.description or d.name)
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
    conv = Conversation(agent=agent, workspace=workspace, max_iteration_per_run=budget_calls * 3,
                        stuck_detection_thresholds={"action_observation": 6, "action_error": 6,
                                                    "monologue": 8, "alternating_pattern": 6})
    if roles == 'none':
        conv.set_confirmation_policy(NeverConfirm())  # v1.54: policy is setter-only, never ctor kwarg
    msg = (brief if brief else spec) or ""
    if contract:
        msg += "\n\nDELIVERABLE CONTRACT (verify each): " + json.dumps(contract, ensure_ascii=False)
    # TDAI read path: shared team memory recall appended when a key is present.
    msg += _recall_block(os.path.basename(workspace.rstrip('/')) + ' ' + (brief or spec or '')[:400])
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
        stage_end(rec, 'team_run', 'fail', exc)
        raise
    stage_end(rec, 'team_run', 'done')

    # deterministic grading: component check (+ pair check vs incumbent) and git
    # state — NOT orchestrator prose
    status, gate_ok, gate_out, pair = evaluate_gate(rec, contract, workspace)

    # lessons gate (owner rule 2026-10-11): every run must end with seeded learnings or an explicit waiver
    stage_start(rec, 'lessons')
    lesson_status = 'WAIVED: ' + waive_lessons if waive_lessons else None
    lesson_err = None
    try:
        if lessons:
            lesson_status = json.dumps(learn_out(workspace, lessons))
        elif not waive_lessons:
            lesson_status = 'LESSONS_REQUIRED: pass lessons=[...] or waive_lessons="reason"'
    except Exception as exc:
        lesson_err = exc
    stage_end(rec, 'lessons', 'fail' if lesson_err else 'done', lesson_err)

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
    }


if __name__ == '__main__':
    print(json.dumps(preflight()[0].name), "preflight ok")
