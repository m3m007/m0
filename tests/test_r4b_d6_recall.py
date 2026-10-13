"""R4b-D6: recall attribution lands in the run record, for all four statuses.

R4 recorded `recall_status`/`recall_hits` flat in config.json. The audit's ask is
sharper: config.json gains a `recall` key shaped
{recall_status: fired|empty|unavailable|error, hits: [{name|sha8, chars}]}, and
`unavailable` must be recorded EXPLICITLY rather than being the silence where a
key used to be. One test per status value, offline, whole run_team() call:

    fired        a hub answer with content   -> hits carry name + chars
    empty        a hub answer with none      -> status empty, hits []
    unavailable  no key / no bridge module   -> status recorded, NOT absent
    error        the hub call itself raised  -> status + the hub's own text

The message handed to the conversation is asserted alongside the record, because
attribution is only worth having if it describes the recall that actually fed the
prompt (R4-D4's point: the block is appended AFTER brief hashing).

Zero network: the bridge is a stub object (tests/test_tdai_bridge.FakeBridge) or
None, and install_oh_stubs() stubs tk._key, so no config.yaml is read and the
hub on :8420 is never contacted.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from team import teamkit as tk  # noqa: E402
from tests.test_tdai_bridge import FakeBridge, capture_sent  # noqa: E402
from tests.test_runrecord import install_oh_stubs  # noqa: E402

STATUSES = ("fired", "empty", "unavailable", "error")


@pytest.fixture(autouse=True)
def runs_env(tmp_path, monkeypatch):
    """Records go to a throwaway dir, never the repo's own runs/."""
    monkeypatch.setenv("TEAMKIT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("TEAMKIT_DRY_RUN", raising=False)


@pytest.fixture
def repo(tmp_path):
    ws = tmp_path / "recallrepo"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", str(ws)], check=False)
    return ws


def read_json(res, name):
    return json.loads((Path(res["run_dir"]) / name).read_text())


def run_recall(monkeypatch, ws, bridge):
    """One whole run with `bridge` as the memory hub (None -> unavailable)."""
    calls = []
    install_oh_stubs(monkeypatch, calls)
    if bridge is not None:
        monkeypatch.setattr(tk, "_tdai", lambda: bridge)
    sent = capture_sent(monkeypatch)
    res = tk.run_team(brief="BRIEF", workspace=str(ws), budget_calls=10,
                      contract={"gate": "true"}, roles="none",
                      waive_lessons="unit test")
    # snapshot, do not hand back the shared SENT list: a second capture_sent()
    # in the same test clears that very object and the first message vanishes
    return res, list(sent)


# ---------------------------------------------------------------- per-status
def test_recall_fired_is_recorded_as_hits_with_names_and_chars(repo, monkeypatch):
    fake = FakeBridge(detail={"hits": ["[tk-lesson] body text"], "status": "fired",
                              "meta": [{"name": "tk-lesson", "chars": 17}]})
    res, sent = run_recall(monkeypatch, repo, fake)
    cfg = read_json(res, "config.json")
    assert cfg["recall"] == {"recall_status": "fired",
                             "hits": [{"name": "tk-lesson", "chars": 17}]}, cfg
    assert "body text" not in json.dumps(cfg), "attribution, never content"
    assert "PRIOR LEARNINGS" in sent[0], "the record matches what was sent"
    assert read_json(res, "summary.json")["recall"] == cfg["recall"], \
        "the record, not just config.json"


def test_recall_empty_is_recorded_as_empty_hits(repo, monkeypatch):
    fake = FakeBridge(detail={"hits": [], "status": "empty", "meta": []})
    res, sent = run_recall(monkeypatch, repo, fake)
    cfg = read_json(res, "config.json")
    assert cfg["recall"]["recall_status"] == "empty"
    assert cfg["recall"]["hits"] == []
    assert "PRIOR LEARNINGS" not in sent[0]
    assert read_json(res, "summary.json")["recall"] == cfg["recall"]


def test_recall_unavailable_is_recorded_explicitly(repo, monkeypatch):
    """No key / no bridge: recorded, not omitted. 'Memory was never reachable'
    has to stay distinguishable from 'memory had nothing to say'."""
    res, sent = run_recall(monkeypatch, repo, None)
    cfg = read_json(res, "config.json")
    assert "recall" in cfg, "unavailable is still recorded"
    assert cfg["recall"] == {"recall_status": "unavailable", "hits": []}, cfg
    assert cfg["recall_status"] == "unavailable", "flat keys stay for old readers"
    assert "PRIOR LEARNINGS" not in sent[0]
    assert read_json(res, "summary.json")["recall"] == cfg["recall"]


def test_recall_error_is_recorded_with_the_hub_text(repo, monkeypatch):
    fake = FakeBridge(raises=True)
    res, sent = run_recall(monkeypatch, repo, fake)
    cfg = read_json(res, "config.json")
    assert cfg["recall"]["recall_status"] == "error", cfg
    assert cfg["recall"]["hits"] == []
    assert "hub exploded" in cfg["recall_error"], cfg
    assert read_json(res, "summary.json")["recall"] == cfg["recall"]


# ------------------------------------------------------------------ contracts
def test_every_run_carries_a_recall_key(repo, monkeypatch):
    """No status may be missing: a record without `recall` cannot be audited for
    whether memory helped, said nothing, or was unreachable."""
    for bridge in (None, FakeBridge(), FakeBridge(raises=True),
                   FakeBridge(detail={"hits": ["h"], "status": "fired",
                                      "meta": [{"sha8": "deadbeef",
                                                "chars": 4}]})):
        res, _ = run_recall(monkeypatch, repo, bridge)
        cfg = read_json(res, "config.json")
        assert cfg["recall"]["recall_status"] in STATUSES, cfg
        assert isinstance(cfg["recall"]["hits"], list)


def test_hash_only_hits_use_sha8(repo, monkeypatch):
    """Atomic-half hits have no name; the record must not invent one."""
    fake = FakeBridge(detail={"hits": ["atomic text"], "status": "fired",
                              "meta": [{"sha8": "ab12cd34", "chars": 11}]})
    res, _ = run_recall(monkeypatch, repo, fake)
    assert read_json(res, "config.json")["recall"]["hits"] == \
        [{"sha8": "ab12cd34", "chars": 11}]


def test_recall_projection_is_independent_of_the_brief_hash(repo, monkeypatch):
    """The attribution survives the block being appended after the hash: same
    brief_sha256, different memory recorded, longer message sent."""
    res_empty, sent_empty = run_recall(
        monkeypatch, repo,
        FakeBridge(detail={"hits": [], "status": "empty", "meta": []}))
    res_fired, sent_fired = run_recall(
        monkeypatch, repo,
        FakeBridge(detail={"hits": ["a long lesson body"], "status": "fired",
                           "meta": [{"name": "tk-x", "chars": 19}]}))
    a, b = read_json(res_empty, "config.json"), read_json(res_fired, "config.json")
    assert a["brief_sha256"] == b["brief_sha256"], "same brief, different memory"
    assert a["recall"]["recall_status"] == "empty"
    assert b["recall"]["recall_status"] == "fired"
    assert len(sent_fired[0]) > len(sent_empty[0]), "the block really was appended"


def test_summary_agrees_with_config_for_every_status(repo, monkeypatch):
    """D6 acceptance as one rule: whatever config.json says about memory,
    summary.json says too."""
    cases = [(None, "unavailable"),
             (FakeBridge(), "empty"),
             (FakeBridge(raises=True), "error"),
             (FakeBridge(detail={"hits": ["x"], "status": "fired",
                                "meta": [{"name": "n1", "chars": 1}]}), "fired")]
    for bridge, status in cases:
        res, _ = run_recall(monkeypatch, repo, bridge)
        cfg, summ = read_json(res, "config.json"), read_json(res, "summary.json")
        assert cfg["recall"]["recall_status"] == status, (status, cfg)
        assert summ["recall"] == cfg["recall"], (status, summ)


def test_recall_summary_projects_and_never_re_derives():
    """_recall_summary is a projection of the _recall_info() contract; content
    cannot leak through it, and no info means no key."""
    assert set(tk.RECALL_STATUSES) == set(STATUSES)
    for status in STATUSES:
        proj = tk._recall_summary({'recall_status': status,
                                   'recall_hits': [{'name': 'n', 'chars': 3}],
                                   'hits': ['body']})
        assert proj == {'recall_status': status,
                        'hits': [{'name': 'n', 'chars': 3}]}, proj
        assert 'body' not in json.dumps(proj)
    assert tk._recall_summary(None) is None
    assert tk._recall_summary({}) is None


def test_returned_dict_key_set_is_unchanged(repo, monkeypatch):
    """Callers that assert the documented keys still pass: `recall` goes into
    the RECORD, never into run_team()'s return value."""
    res, _ = run_recall(monkeypatch, repo, FakeBridge())
    assert "_recall_info" not in res, "the private carrier is popped"
    assert set(res) == {"status", "gate", "gate_tail", "lessons",
                        "workdir_diff", "recent_commits", "orchestrator_final",
                        "run_dir", "pair", "outcome", "work_evidence"}, sorted(res)
