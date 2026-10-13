"""Offline tests for the R4 memory-loop repair: bridge + teamkit wiring.

Why these exist. The write path was provably dead: lessons were named
`teamkit-lesson-<slot>`, so slot 0 could be written once ever and every later
run got rejected by the hub (error 42201) while the stage still reported done.
The read path was half-dead: recall failures no-op'd into the same silence as an
empty memory, and nothing recorded what memory contributed.

Zero network, by construction: `urllib.request.urlopen` is monkeypatched with a
fake that records the *Request objects*, so every assertion is about the URL,
the headers and the body JSON that would have gone out. The hub on :8420 is
never touched, and no test needs a key (bridge `_key` is patched per test).

Layout:
  A  D1 naming          unique across contents/runs, idempotent within one
  B  D2 request shape   identity in every body, both header families, top_k+scope
  C  D4 recall status    fired|empty|error from the bridge, detail shape
  D  D1/D3/D4 teamkit   whole-run stubs: seed policy, partial stage, config keys
  E  D5 tester.md       the budget one-liner over a fixture ledger
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from team import teamkit as tk  # noqa: E402  (repo root above)
from tests.test_runrecord import install_oh_stubs  # noqa: E402

BRIDGE = REPO / "team" / "tdai-bridge.py"
BRIDGE_TWIN = Path("/home/hermes/.hermes/scripts/tdai-bridge.py")
KEY = "test-admin-key-never-real"


# --------------------------------------------------------------------- harness
def bearer_prefix():
    """The literal prefix on the bridge's Authorization line.

    Out of scope for R4 and verified correct on disk, but read back from the
    source rather than hard coded: this harness display-redacts that word, so a
    literal here would fail for the wrong reason.
    """
    line = next(ln for ln in BRIDGE.read_text().splitlines()
                if '"Authorization"' in ln)
    m = re.search(r'"Authorization": "([^"]*)" \+ _key\(\)', line)
    assert m, line
    return m.group(1)


def load_bridge(monkeypatch, key=KEY):
    """A fresh module instance of the repo-owned bridge, key stubbed."""
    spec = importlib.util.spec_from_file_location("tdai_bridge_under_test", str(BRIDGE))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_key", lambda: key)
    return mod


class FakeResp:
    def __init__(self, payload):
        self._bytes = json.dumps(payload).encode()

    def read(self):
        return self._bytes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHub:
    """Stand-in for urllib.request.urlopen: records requests, replies canned.

    `responses` maps a path substring to the JSON envelope; anything unmatched
    replies {"code": 0, "data": {}}. `boom` raises instead, which is how a dead
    hub is exercised without a socket.
    """

    def __init__(self, responses=None, boom=None):
        self.requests = []            # list of dicts: url/method/headers/body
        self.responses = responses or {}
        self.boom = boom

    def __call__(self, req, timeout=None):
        body = req.data.decode() if req.data else ""
        self.requests.append({
            "url": req.full_url,
            "path": req.full_url.split("://", 1)[-1].split("/", 1)[-1],
            "method": req.get_method(),
            "headers": {k.lower(): v for k, v in req.header_items()},
            "body": json.loads(body) if body else {},
            "timeout": timeout,
        })
        for frag, payload in self.responses.items():
            if frag in req.full_url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResp(payload)
        if self.boom:
            raise self.boom
        return FakeResp({"code": 0, "data": {}})

    # --- assertion helpers
    def paths(self):
        return [r["path"] for r in self.requests]

    def by_path(self, frag):
        return [r for r in self.requests if frag in r["url"]]

    def bodies(self, frag):
        return [r["body"] for r in self.by_path(frag)]

    def headers(self, frag=None):
        reqs = self.by_path(frag) if frag else self.requests
        assert reqs, f"no request captured for {frag!r}"
        return reqs[0]["headers"]


@pytest.fixture
def hub(monkeypatch):
    """Patch urlopen process-wide; nothing in these tests can reach the network."""
    fake = FakeHub()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


# ------------------------------------------------- D1: lesson names (bridge API)
def test_lesson_name_uses_workspace_slug_and_content_sha8(monkeypatch):
    tb = load_bridge(monkeypatch)
    name = tb.lesson_name("/home/hermes/projects/multiagent-stack", "a lesson")
    assert name.startswith("lesson-multiagent-stack-")
    sha = name.rsplit("-", 1)[-1]
    assert re.fullmatch(r"[0-9a-f]{8}", sha), name


def test_two_different_contents_get_two_names(monkeypatch):
    """The dead scheme gave both notes slot 0; the hub then rejected the second."""
    tb = load_bridge(monkeypatch)
    a = tb.lesson_name("/x/myproj", "lesson about lanes")
    b = tb.lesson_name("/x/myproj", "lesson about gates")
    assert a != b


def test_same_content_is_idempotent_across_workspaces_and_runs(monkeypatch):
    tb = load_bridge(monkeypatch)
    assert tb.lesson_name("/a/myproj", "same text") == tb.lesson_name("/b/myproj", "same text")
    assert tb.lesson_name("/a/myproj", "same text\n") != tb.lesson_name("/a/myproj", "same text")


def test_ws_slug_is_lowercased_dashed_and_capped_32(monkeypatch):
    tb = load_bridge(monkeypatch)
    assert tb.ws_slug("/home/x/My_Project-2026!!") == "my-project-2026"
    long = tb.ws_slug("/tmp/" + "A" * 80)
    assert len(long) <= 32
    assert tb.ws_slug("/tmp/***") == "workspace"


def test_seed_lessons_accepts_two_distinct_contents(tmp_path, monkeypatch, hub):
    hub.responses = {"/v3/skill/create": {"code": 0, "data": {"name": "ok"}}}
    tb = load_bridge(monkeypatch)
    res = tb.seed_lessons(str(tmp_path / "myproj"), ["lesson one", "lesson two"])
    assert res["seeded"] == 2 and res["errors"] == 0 and res["duplicates"] == 0
    assert len(set(res["names"])) == 2, "one run must not reuse a name"
    assert len(hub.by_path("/v3/skill/create")) == 2


def test_seed_lessons_repeating_same_content_is_duplicate_not_crash(tmp_path, monkeypatch, hub):
    """Same lesson twice: second write rejected (42201), learn_out survives it."""
    seen = []

    def reply(req, timeout=None):
        body = json.loads(req.data.decode())
        if body["name"] in seen:
            return FakeResp({"code": 42201, "message": "skill name already exists"})
        seen.append(body["name"])
        return FakeResp({"code": 0, "data": {"name": body["name"]}})

    monkeypatch.setattr(urllib.request, "urlopen", reply)
    tb = load_bridge(monkeypatch)
    notes = ["lesson alpha", "lesson beta"]
    first = tb.seed_lessons(str(tmp_path / "myproj"), notes)
    second = tb.seed_lessons(str(tmp_path / "myproj"), notes)   # must not raise
    assert first["seeded"] == 2, first
    assert second["seeded"] == 0 and second["duplicates"] == 2, second


def test_seed_skill_reports_false_on_error_without_raising(monkeypatch, hub):
    hub.boom = OSError("connection refused")
    tb = load_bridge(monkeypatch)
    assert tb.seed_skill("lesson-x-abcdef01", "content") is False


# --------------------------------------------- D2: identity, headers, search shape
def test_call_merges_identity_into_every_payload(monkeypatch, hub):
    tb = load_bridge(monkeypatch)
    for path in ("/v3/skill/search", "/v3/atomic/search",
                 "/v3/skill/get-by-name", "/v3/skill/create"):
        tb.call(path, {"query": "q"})
    assert len(hub.requests) == 4, "one request per path"
    for req in hub.requests:
        for k in ("team_id", "agent_id", "user_id"):
            assert req["body"].get(k) == tb.identity()[k], (req["url"], req["body"])


def test_skill_search_sends_top_k_limit_and_team_scope(monkeypatch, hub):
    """Doc field is top_k; limit is what the old bridge sent. Send both."""
    tb = load_bridge(monkeypatch)
    tb.recall_detail("gate policy", limit=7)
    bodies = hub.bodies("/v3/skill/search")
    assert len(bodies) == 1
    b = bodies[0]
    assert b["top_k"] == 7 and b["limit"] == 7
    assert b["scope"] == "team"
    atomic = hub.bodies("/v3/atomic/search")[0]
    assert atomic["top_k"] == 7 and atomic["scope"] == "team"


def test_headers_carry_both_families_and_bearer_untouched(monkeypatch, hub):
    tb = load_bridge(monkeypatch)
    tb.call("/v3/skill/search", {"query": "q"})
    h = hub.headers()
    legacy = ("x-team-id", "x-agent-id", "x-user-id")
    tdai = ("x-tdai-team-id", "x-tdai-agent-id", "x-tdai-user-id")
    for k in legacy + tdai:
        assert h.get(k), f"missing header {k}: {sorted(h)}"
    assert h["x-team-id"] == h["x-tdai-team-id"] == tb.TEAM
    assert h["x-agent-id"] == h["x-tdai-agent-id"] == tb.AGENT
    assert h["x-user-id"] == h["x-tdai-user-id"] == tb.USER
    # out-of-scope guard: the Bearer line is correct on disk, prove it stays so
    assert h["authorization"] == bearer_prefix() + KEY
    assert h["x-tdai-service-id"] == tb.SERVICE_ID


def test_get_by_name_and_create_bodies_have_identity_and_task(tmp_path, monkeypatch, hub):
    hub.responses = {"/v3/skill/create": {"code": 0, "data": {"name": "n"}}}
    tb = load_bridge(monkeypatch)
    tb.skill_body("lesson-x-abcdef01")
    tb.seed_skill("lesson-x-abcdef01", "the lesson text")
    got = hub.bodies("/v3/skill/get-by-name")[0]
    created = hub.bodies("/v3/skill/create")[0]
    for b in (got, created):
        assert {"team_id", "agent_id", "user_id"} <= set(b), b
    assert created["task_id"] == "task-p4139enx1n"
    assert created["content"].lstrip().startswith("---"), "frontmatter added when absent"


def test_recall_detail_fires_and_returns_meta_without_content(monkeypatch, hub):
    hub.responses = {
        "/v3/skill/search": {"code": 0, "data": {"items": [
            {"name": "tk-lesson", "description": "a remembered lesson"}]}},
        "/v3/skill/get-by-name": {"code": 0, "data": {"content": "full body"}},
        "/v3/atomic/search": {"code": 0, "data": {"items": []}},
    }
    tb = load_bridge(monkeypatch)
    d = tb.recall_detail("lesson")
    assert d["status"] == "fired"
    assert d["hits"][0].startswith("[tk-lesson]"), d["hits"]
    assert d["meta"] == [{"name": "tk-lesson", "chars": len(d["hits"][0])}]
    blob = json.dumps(d["meta"])
    assert "full body" not in blob, "meta must never carry content"


def test_recall_detail_empty_vs_error_are_distinguishable(monkeypatch, hub):
    tb = load_bridge(monkeypatch)
    hub.responses = {}
    assert tb.recall_detail("nothing")["status"] == "empty"
    hub.boom = OSError("hub down")
    assert tb.recall_detail("anything")["status"] == "error"
    assert tb.recall("anything") == [], "list API stays safe"


def test_error_envelope_surfaces_as_error_not_silence(monkeypatch, hub):
    hub.responses = {"/v3/skill/search": {"code": 50001, "message": "agent not found"}}
    tb = load_bridge(monkeypatch)
    d = tb.recall_detail("q")
    assert d["status"] == "error", "a hub error dict must not read as empty memory"


# ------------------------------------------------ teamkit-level stubs (D3 / D4)
class FakeBridge:
    """A stand-in bridge module object for whole-run tests.

    Counts every hub touch, so "zero hub calls" is an assertion rather than an
    absence of evidence.
    """

    def __init__(self, detail=None, seeds=None, raises=False):
        self.detail = detail or {"hits": [], "status": "empty", "meta": []}
        self.seeds = seeds
        self.raises = raises
        self.recall_calls = []
        self.seed_calls = []

    def available(self):
        return True

    def recall_detail(self, query, limit=6):
        self.recall_calls.append(query)
        if self.raises:
            raise RuntimeError("hub exploded")
        return dict(self.detail)

    def lesson_name(self, workspace, content):
        return f"lesson-{os.path.basename(str(workspace))}-{tb_sha8(content)}"

    def seed_lessons(self, workspace, notes):
        """Mimic the bridge batch API: one call, per-note outcome counts."""
        self.seed_calls.append((workspace, list(notes)))
        res = {"names": [self.lesson_name(workspace, n) for n in notes],
               "seeded": len(notes), "duplicates": 0, "errors": 0,
               "of": len(notes)}
        res.update(self.seeds or {})
        return res


def tb_sha8(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def git(*args, cwd):
    return subprocess.run(["git", "-c", "user.email=t@local",
                           "-c", "user.name=t", *args],
                          cwd=str(cwd), capture_output=True, text=True,
                          check=False)


@pytest.fixture(autouse=True)
def runs_env(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(root))
    monkeypatch.delenv("TEAMKIT_DRY_RUN", raising=False)
    return root


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "targetrepo"
    ws.mkdir()
    git("init", "-q", cwd=ws)
    Path(ws, "tracked.txt").write_text("baseline\n")
    git("add", "tracked.txt", cwd=ws)
    git("commit", "-qm", "baseline", cwd=ws)
    return ws


SENT = []


def capture_sent(monkeypatch):
    """Make the stubbed Conversation record the message it was handed."""
    SENT.clear()
    mod = sys.modules["openhands.sdk"]
    base = mod.Conversation

    class Capturing(base):
        def send_message(self, msg):
            SENT.append(msg)
            super().send_message(msg)

    monkeypatch.setattr(mod, "Conversation", Capturing)
    return SENT


def run_with_bridge(monkeypatch, ws, fake, contract=None, calls=None, **kw):
    """One whole run_team() with openhands + the bridge stubbed."""
    calls = calls if calls is not None else []
    install_oh_stubs(monkeypatch, calls)
    monkeypatch.setattr(tk, "_tdai", lambda: fake)
    kw.setdefault("budget_calls", 10)
    kw.setdefault("roles", "none")
    return tk.run_team(brief="BRIEF", workspace=str(ws),
                       contract=contract or {"gate": "true"}, **kw)


def read_config(res):
    return json.loads((Path(res["run_dir"]) / "config.json").read_text())


def read_state(res):
    return json.loads((Path(res["run_dir"]) / "state.json").read_text())


# ---------------------------------------------------------------- D3: seed policy
def test_abstained_run_seeds_nothing_and_says_skipped(repo, monkeypatch):
    """The dead-path fix: a run that did no work must not push junk memory."""
    fake = FakeBridge()
    calls = []
    res = run_with_bridge(monkeypatch, repo, fake,
                          contract={"gate": "false"}, calls=calls,
                          lessons=["a lesson from a stalled run"])
    assert res["outcome"] == "abstained", res["outcome"]
    assert fake.seed_calls == [], "zero hub writes on a non-retained outcome"
    assert res["lessons"] == "SKIPPED_NOT_RETAINED: outcome=abstained"
    assert read_state(res)["lessons"]["status"] == "partial"


def test_partial_work_run_also_refuses_to_seed(repo, monkeypatch):
    fake = FakeBridge()
    res = run_with_bridge(monkeypatch, repo, fake, contract={"gate": "false"},
                          lessons=["junk"],
                          on_run=lambda ws: Path(ws, "uncommitted.txt").write_text("x\n"))
    assert res["outcome"] == "partial_work"
    assert fake.seed_calls == []
    assert res["lessons"].startswith("SKIPPED_NOT_RETAINED:")


def test_success_run_seeds_lessons(repo, monkeypatch):
    fake = FakeBridge()
    res = run_with_bridge(monkeypatch, repo, fake, contract={"gate": "true"},
                          lessons=["lesson one", "lesson two"])
    assert res["outcome"] == "success"
    assert len(fake.seed_calls) == 1, "exactly one batch write"
    assert fake.seed_calls[0][1] == ["lesson one", "lesson two"]
    payload = json.loads(res["lessons"])
    assert payload["seeded"] == 2 and payload["of"] == 2
    assert read_state(res)["lessons"]["status"] == "done"


def test_partial_seeding_marks_the_stage_partial(repo, monkeypatch):
    """Never silently done when fewer lessons landed than were handed over."""
    fake = FakeBridge(seeds={"seeded": 1, "duplicates": 1, "errors": 0})
    res = run_with_bridge(monkeypatch, repo, fake, contract={"gate": "true"},
                          lessons=["a", "b"])
    assert read_state(res)["lessons"]["status"] == "partial"
    assert res["lessons"] != "done"


def test_all_duplicates_is_partial_too(repo, monkeypatch):
    fake = FakeBridge(seeds={"seeded": 0, "duplicates": 2, "errors": 0})
    res = run_with_bridge(monkeypatch, repo, fake, contract={"gate": "true"},
                          lessons=["a", "b"])
    assert read_state(res)["lessons"]["status"] == "partial", "the old silent no-op"


def test_explicit_empty_lessons_is_a_waiver(repo, monkeypatch):
    fake = FakeBridge()
    res = run_with_bridge(monkeypatch, repo, fake, contract={"gate": "false"},
                          lessons=[])
    assert fake.seed_calls == []
    assert res["lessons"] == "WAIVED: lessons=[] (explicit)"
    assert read_state(res)["lessons"]["status"] == "done"


def test_tie_kept_run_may_seed(repo, monkeypatch):
    """SEEDABLE_OUTCOMES both members: a tie is retained work, so it teaches."""
    fake = FakeBridge()
    res = run_with_bridge(monkeypatch, repo, fake,
                          contract={"gate": "true", "incumbent_gate": "true"},
                          lessons=["a lesson from retained work"])
    assert res["outcome"] == "tie_kept", res["outcome"]
    assert len(fake.seed_calls) == 1


def test_skipped_path_writes_nothing_at_all(repo, monkeypatch):
    """The hub stays untouched on a non-seedable outcome: no batch, no per-note."""
    fake = FakeBridge()
    calls = []
    res = run_with_bridge(monkeypatch, repo, fake, contract={"gate": "false"},
                          calls=calls, lessons=["junk"], waive_lessons=None)
    assert fake.seed_calls == [] and res["lessons"].startswith("SKIPPED_NOT_RETAINED")
    assert json.loads((Path(res["run_dir"]) / "summary.json").read_text())["lessons"] == res["lessons"]


def test_hub_down_run_is_unchanged_and_stage_done(repo, monkeypatch):
    """Backward compat: no bridge means the old shape, byte-compatible keys."""
    calls = []
    install_oh_stubs(monkeypatch, calls)      # installs _tdai -> None
    res = tk.run_team(brief="BRIEF", workspace=str(repo), budget_calls=10,
                      contract={"gate": "true"}, roles="none",
                      lessons=["would-be lesson"])
    cfg = read_config(res)
    assert cfg["recall_status"] == "unavailable" and cfg["recall_hits"] == []
    payload = json.loads(res["lessons"])
    assert payload["tdai"] == "unavailable"
    assert read_state(res)["lessons"]["status"] == "done"


# ------------------------------------------------------------- D4: attribution
def test_recall_fired_records_names_and_chars_and_appends_block(repo, monkeypatch):
    fake = FakeBridge(detail={"hits": ["[tk-lesson] body"],
                              "status": "fired",
                              "meta": [{"name": "tk-lesson", "chars": 17}]})
    calls = []
    install_oh_stubs(monkeypatch, calls)
    monkeypatch.setattr(tk, "_tdai", lambda: fake)
    capture_sent(monkeypatch)
    res = tk.run_team(brief="BRIEF", workspace=str(repo), budget_calls=10,
                      contract={"gate": "true"}, roles="none",
                      waive_lessons="unit test")
    cfg = read_config(res)
    assert cfg["recall_status"] == "fired"
    assert cfg["recall_hits"] == [{"name": "tk-lesson", "chars": 17}]
    blob = json.dumps(cfg)
    assert "body" not in blob, "config.json holds attribution, never content"
    assert "PRIOR LEARNINGS" in SENT[0]


@pytest.mark.parametrize("status", ["empty", "error", "unavailable"])
def test_recall_status_is_recorded_for_every_outcome(repo, monkeypatch, status):
    """The four outcomes were indistinguishable; each gets its own label."""
    calls = []
    install_oh_stubs(monkeypatch, calls)
    if status == "unavailable":
        monkeypatch.setattr(tk, "_tdai", lambda: None)
    else:
        fake = FakeBridge(detail={"hits": [], "status": status, "meta": []})
        monkeypatch.setattr(tk, "_tdai", lambda: fake)
    capture_sent(monkeypatch)
    res = tk.run_team(brief="BRIEF", workspace=str(repo), budget_calls=10,
                      contract={"gate": "true"}, roles="none",
                      waive_lessons="unit test")
    cfg = read_config(res)
    assert cfg["recall_status"] == status
    assert cfg["recall_hits"] == []
    assert "PRIOR LEARNINGS" not in SENT[0], "only a fired recall touches the message"


def test_exploding_bridge_is_error_not_empty(repo, monkeypatch):
    calls = []
    install_oh_stubs(monkeypatch, calls)
    monkeypatch.setattr(tk, "_tdai", lambda: FakeBridge(raises=True))
    capture_sent(monkeypatch)
    res = tk.run_team(brief="BRIEF", workspace=str(repo), budget_calls=10,
                      contract={"gate": "true"}, roles="none",
                      waive_lessons="unit test")
    cfg = read_config(res)
    assert cfg["recall_status"] == "error"
    assert "hub exploded" in cfg["recall_error"]


def test_recall_status_vocabulary_is_enforced(repo, monkeypatch):
    """RECALL_STATUSES is the contract, not a comment: a bridge that answers with
    something outside it is recorded as `error`, never smuggled into config."""
    for given, expected in [("mystery", "error"), ("fired", "empty"),
                            ("", "empty"), (None, "empty")]:
        calls = []
        install_oh_stubs(monkeypatch, calls)
        monkeypatch.setattr(tk, "_tdai", lambda g=given: FakeBridge(
            detail={"hits": [], "status": g, "meta": []}))
        res = tk.run_team(brief="B", workspace=str(repo), budget_calls=10,
                          contract={"gate": "true"}, roles="none",
                          waive_lessons="unit test")
        assert read_config(res)["recall_status"] == expected, given
    assert set(tk.RECALL_STATUSES) == {"fired", "empty", "unavailable", "error"}


def test_hub_error_text_reaches_config_json_through_the_real_bridge(repo, monkeypatch, hub):
    """D4 end to end with the REAL bridge: a hub that answers 50001 is recorded
    as `error` with the hub's own text, not as the silence of an empty memory."""
    hub.responses = {"/v3/skill/search": {"code": 50001,
                                          "message": "agent not found"},
                     "/v3/atomic/search": {"code": 0, "data": {"items": []}}}
    real = load_bridge(monkeypatch)
    calls = []
    install_oh_stubs(monkeypatch, calls)
    monkeypatch.setattr(tk, "_tdai", lambda: real)
    capture_sent(monkeypatch)
    res = tk.run_team(brief="BRIEF", workspace=str(repo), budget_calls=10,
                      contract={"gate": "true"}, roles="none",
                      waive_lessons="unit test")
    cfg = read_config(res)
    assert cfg["recall_status"] == "error", cfg
    assert "50001" in cfg["recall_error"], cfg
    assert cfg["recall_hits"] == []


def test_recall_query_is_workspace_plus_brief_prefix(repo, monkeypatch):
    fake = FakeBridge()
    run_with_bridge(monkeypatch, repo, fake, waive_lessons="unit test")
    assert fake.recall_calls == ["targetrepo BRIEF"], fake.recall_calls


def test_learn_out_names_lessons_by_content(tmp_path, monkeypatch, hub):
    """learn_out through the REAL bridge: the D1 fix end to end, offline."""
    hub.responses = {"/v3/skill/create": {"code": 0, "data": {"name": "ok"}}}
    real = load_bridge(monkeypatch)
    monkeypatch.setattr(tk, "TDAI_BRIDGE", str(BRIDGE))
    monkeypatch.setattr(tk, "_tdai", lambda: real)
    out = tk.learn_out(str(tmp_path / "myproj"), ["lesson A", "lesson B"])
    assert out["seeded"] == 2
    names = hub.bodies("/v3/skill/create")
    assert [n["name"] for n in names] == out["names"]
    assert all(n["name"].startswith("lesson-myproj-") for n in names), names
    assert len({n["name"] for n in names}) == 2


def test_learn_out_twice_with_same_content_never_crashes(tmp_path, monkeypatch, hub):
    """The brief's D1 offline case, through teamkit's own entry point."""
    seen = []

    def reply(req, timeout=None):
        body = json.loads(req.data.decode())
        if body["name"] in seen:
            return FakeResp({"code": 42201, "message": "skill name already exists"})
        seen.append(body["name"])
        return FakeResp({"code": 0, "data": {"name": body["name"]}})

    monkeypatch.setattr(urllib.request, "urlopen", reply)
    real = load_bridge(monkeypatch)
    monkeypatch.setattr(tk, "_tdai", lambda: real)
    ws = str(tmp_path / "myproj")
    first = tk.learn_out(ws, ["shared lesson", "another lesson"])
    second = tk.learn_out(ws, ["shared lesson"])          # must not raise
    third = tk.learn_out(ws, ["shared lesson", "another lesson"])
    assert (first["seeded"], first["of"]) == (2, 2), first
    assert (second["seeded"], second["duplicates"]) == (0, 1), second
    assert (third["seeded"], third["duplicates"]) == (0, 2), third
    assert len(seen) == 2, "the hub holds exactly the two distinct lessons"


def test_abstained_run_makes_no_write_call_at_all(repo, monkeypatch, hub):
    """The literal D3 guarantee: real bridge, real request capture, and the
    create endpoint is never touched when the outcome is not retained."""
    real = load_bridge(monkeypatch)
    hub.responses = {"/v3/skill/search": {"code": 0, "data": {"items": []}},
                     "/v3/atomic/search": {"code": 0, "data": {"items": []}}}
    calls = []
    install_oh_stubs(monkeypatch, calls)
    monkeypatch.setattr(tk, "_tdai", lambda: real)
    res = tk.run_team(brief="BRIEF", workspace=str(repo), budget_calls=10,
                      contract={"gate": "false"}, roles="none",
                      lessons=["junk from a stalled run"])
    assert res["outcome"] == "abstained"
    assert res["lessons"] == "SKIPPED_NOT_RETAINED: outcome=abstained"
    assert hub.by_path("/v3/skill/create") == [], [r["url"] for r in hub.requests]
    assert read_state(res)["lessons"]["status"] == "partial"


# ------------------------------------------------------------------ D5: tester
def _budget_one_liner():
    text = (REPO / "team" / "roles" / "tester.md").read_text()
    lines = [ln.strip() for ln in text.splitlines() if "python3 -c" in ln]
    assert len(lines) == 1, f"expected exactly one budget line, got {len(lines)}"
    m = re.match(r'^python3 -c "(.*)"$', lines[0].replace("TASK_START_TS=$(date +%s) ", ""))
    assert m, lines[0]
    return m.group(1)


def _run_one_liner(ledger, start_ts=None):
    env = dict(os.environ, MULTIAGENT_LEDGER_PATH=str(ledger))
    env.pop("TASK_START_TS", None)
    if start_ts is not None:
        env["TASK_START_TS"] = str(start_ts)
    p = subprocess.run([sys.executable, "-c", _budget_one_liner()],
                       capture_output=True, text=True, env=env, check=False)
    assert p.returncode == 0, p.stderr
    return p.stdout


@pytest.fixture
def fixture_ledger(tmp_path):
    now = time.time()
    rows = [
        {"ts": now - 4000, "source": "multiagent-stack:router:fixer", "calls": 10},
        {"ts": now - 3000, "source": "multiagent-stack:router:tester", "calls": 5},
        {"ts": now - 100, "source": "multiagent-stack:router:reviewer", "calls": 7},
        {"ts": now - 50, "source": "multiagent-stack:manual", "calls": 3},
        {"ts": now - 20, "source": "other-project:router:fixer", "calls": 99},
    ]
    path = tmp_path / "calls.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_one_liner_windows_the_spend_and_prints_both_labels(fixture_ledger):
    out = _run_one_liner(fixture_ledger, time.time() - 200)
    assert "spent_since_start 10" in out, out
    assert "session_cumulative 25" in out, out
    assert "ONLY" in out


def test_one_liner_without_window_counts_the_whole_session(fixture_ledger):
    out = _run_one_liner(fixture_ledger)
    assert "spent_since_start 25" in out, out
    assert "session_cumulative 25" in out, out
    assert "TASK_START_TS unset" in out, "silence about the window is a lie"


def test_one_liner_tolerates_missing_ts_and_calls_keys(tmp_path):
    p = tmp_path / "sparse.jsonl"
    p.write_text('{"ts": 5, "source": "multiagent-stack:x"}\n'
                 '{"ts": 9, "calls": 2, "source": "multiagent-stack:y"}\n')
    out = _run_one_liner(p, 5)
    assert "spent_since_start 3" in out, out + " (rows missing calls count as 1)"
    assert "session_cumulative 3" in out, out


def test_one_liner_reads_the_real_ledger_and_starts_at_zero():
    """Read-only sanity on the real file: a window that starts in the future
    must report zero spend, and the cumulative must be a plausible total.
    Reads only; no row is written (the ledger lives outside the repo)."""
    real = Path(os.environ["HOME"]) / ".hermes" / "quota" / "calls.jsonl"
    if not real.is_file():
        pytest.skip("no ledger on this machine")
    env = dict(os.environ, TASK_START_TS=str(time.time() + 3600))
    env.pop("MULTIAGENT_LEDGER_PATH", None)
    p = subprocess.run([sys.executable, "-c", _budget_one_liner()],
                       capture_output=True, text=True, env=env, check=False)
    assert p.returncode == 0, p.stderr
    assert "spent_since_start 0" in p.stdout, p.stdout
    cum = int(re.search(r"session_cumulative (\d+)", p.stdout).group(1))
    assert cum == sum(int(json.loads(l).get("calls", 1))
                      for l in real.read_text().splitlines() if l.strip()
                      and json.loads(l).get("source", "").startswith("multiagent-stack"))


def test_tester_role_still_parses_and_frontmatter_leads():
    """The role file is config: line 1 must open the YAML block (RC1)."""
    from team import run_team as rtm

    text = (REPO / "team" / "roles" / "tester.md").read_text()
    assert text.startswith("---\n"), "frontmatter must begin on line 1"
    assert "\u2014" not in text, "published docs use no em dashes"
    defs = rtm.load_roles(REPO / "team" / "roles")
    assert any(d.name == "tester" for d in defs), [d.name for d in defs]
    tester = next(d for d in defs if d.name == "tester")
    assert tester.tools, "the repaired line must not break the role definition"


# --------------------------------------------------------------- repo contracts
def test_executor_twin_is_byte_identical_to_repo_copy():
    """The bridge lives outside the repo; drift between the two is drift in prod."""
    assert BRIDGE_TWIN.is_file(), BRIDGE_TWIN
    assert BRIDGE_TWIN.read_bytes() == BRIDGE.read_bytes(), (
        "team/tdai-bridge.py and ~/.hermes/scripts/tdai-bridge.py must match; "
        "copy the repo file over the twin")


def test_teamkit_imports_the_repo_copy():
    assert tk.TDAI_BRIDGE == str(BRIDGE)
    assert tk.TDAI_BRIDGE_EXECUTOR == str(BRIDGE_TWIN)
    loaded = importlib.util.spec_from_file_location("t", tk.TDAI_BRIDGE)
    assert loaded and loaded.origin != "built-in"


def test_bridge_never_prints_key_material():
    src = BRIDGE.read_text()
    assert "print(_key" not in src and "print(key" not in src
    assert "_key()" in src, "still resolves the key"
