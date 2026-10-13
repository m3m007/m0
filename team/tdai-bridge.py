#!/usr/bin/env python3
"""TDAI memory hub bridge for teamkit (read/write paths).

Repo-owned copy: this is the source of truth, and the executor-readable twin at
~/.hermes/scripts/tdai-bridge.py is kept byte-identical to it, so any caller can
read the code that actually ran while `git log` still explains the memory path.
teamkit imports THIS file (TDAI_BRIDGE points here).

Key resolution order:
  1. ~/.hermes/secrets-tdai.key          (preferred once owner places it)
  2. TDAI_ADMIN_KEY env var
If neither exists, functions no-op silently with `available=False` — runs
proceed exactly as today (zero regression). Gateway is loopback-only (:8420).
Never prints key material.

R4 repairs, each against the failure it closes:
  D1 names      `teamkit-lesson-<slot>` was unique per RUN, not per history, so
                slot 0 could be written once ever and every later run was
                rejected (hub error 42201) while the stage still reported done.
                Names are now `lesson-<workspace-slug>-<sha8-of-content>`.
  D2 identity   every body carries team_id + agent_id + user_id (merged in
                `call()`, so a new endpoint cannot forget them), BOTH header
                families go out (the bare shape is pilot-proven; the x-tdai-*
                one the hub doc names is additive, never a replacement), and
                skill/search uses the documented `top_k` next to `limit`,
                plus `scope: "team"`.
  D4 recall     `recall_detail()` distinguishes fired|empty|error so a hub that
                threw is never reported as "the team had nothing to remember".
"""
import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path

GATEWAY = "http://127.0.0.1:8420"
SERVICE_ID = "default"
# pilot identities (not secrets; recorded in multiagent-stack/runs/DONE.md)
TEAM = "team-p413zjs4su"
AGENT = "agt-p413hsy3o7"
USER = "usr-p4wi8pndrm"
TASK = "task-p4139enx1n"        # the pilot task every lesson is filed under
DUP_CODE = "42201"              # hub: skill name already taken
SLUG_MAX = 32                   # workspace slug length ceiling (D1)


def _key():
    p = Path.home() / ".hermes/secrets-tdai.key"
    if p.is_file():
        return p.read_text().strip()
    return os.environ.get("TDAI_ADMIN_KEY", "")


def available():
    return bool(_key())


def identity():
    """The three scoping ids every request body must carry (D2)."""
    return {"team_id": TEAM, "agent_id": AGENT, "user_id": USER}


def headers():
    """Both header families (D2). Additive on purpose: pilot M1 proved the bare
    `x-team-id/x-agent-id/x-user-id` shape end-to-end, so removing it would be a
    regression; the hub kernel doc names `x-tdai-*`, so those are added."""
    return {"Content-Type": "application/json",
            "Authorization": "Bearer " + _key(),
            "x-tdai-service-id": SERVICE_ID,
            "x-team-id": TEAM, "x-agent-id": AGENT, "x-user-id": USER,
            "x-tdai-team-id": TEAM, "x-tdai-agent-id": AGENT,
            "x-tdai-user-id": USER}


def call(path, payload):
    """POST path with payload; the identity ids are merged here, never by the
    caller, so no endpoint can forget them (D2).

    Returns the hub's `data` dict, or {"_error": ...}. Callers must surface that
    rather than swallow it (D4): a dead hub and an empty memory are different
    facts.
    """
    body = dict(payload or {})
    for k, v in identity().items():
        body.setdefault(k, v)
    req = urllib.request.Request(GATEWAY + path,
                                 data=json.dumps(body).encode(),
                                 headers=headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return {"_error": str(e)[:200]}
    if data.get("code") not in (0, None) and "data" not in data:
        return {"_error": str(data)[:200]}
    return data.get("data", data)


def is_error(res):
    return isinstance(res, dict) and "_error" in res


def is_duplicate(res):
    """True when `res` is the hub's already-this-name rejection (42201)."""
    if not is_error(res):
        return False
    txt = str(res.get("_error") or "").lower()
    return (DUP_CODE in txt or "already exist" in txt
            or "duplicate name" in txt or "name is taken" in txt)


# ------------------------------------------------------------------ D1: naming
def ws_slug(workspace):
    """basename(workspace) lowercased, non-alnum -> '-', max 32 chars."""
    s = re.sub(r"[^a-z0-9]+", "-",
               os.path.basename(str(workspace or "").rstrip("/")).lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")[:SLUG_MAX].strip("-")
    return s or "workspace"


def content_sha8(content):
    return hashlib.sha256((content or "").encode("utf-8", "replace")).hexdigest()[:8]


def lesson_name(workspace, content):
    """Unique-per-content lesson name (D1).

    The hash is of the lesson TEXT, not of a slot index: two different lessons
    from one run get two different names, and re-submitting the same lesson is
    the hub's duplicate-name rejection instead of a silent overwrite.
    """
    return f"lesson-{ws_slug(workspace)}-{content_sha8(content)}"


# ------------------------------------------------------------------ read path
def skill_body(name):
    """Fetch full SKILL.md content for a searched skill name (search omits body)."""
    d = call("/v3/skill/get-by-name", {"skill_name": name})
    if is_error(d):
        return ""
    return (d or {}).get("content") or ""


def recall(query, limit=6):
    """Skill-asset search (proven M1 lane) + L1 atomic -> list of content strings.

    The old list shape, kept for callers that only want hits (the M1 harness).
    teamkit uses recall_detail(), which can tell `empty` from `error`.
    """
    return recall_detail(query, limit)["hits"]


def recall_detail(query, limit=6):
    """-> {"hits": [...], "status": fired|empty|error, "meta": [{name|sha8,chars}]}.

    `meta` holds names/hashes and sizes only, never content, so config.json can
    record what memory contributed without copying memory into the run record.
    Skill records carry full SKILL.md in `content` via get-by-name; search
    returns names+descriptions, which are the actionable part.
    """
    hits, meta, errored = [], [], False
    errors = []
    d = call("/v3/skill/search", {"query": query, "limit": limit, "top_k": limit,
                                  "scope": "team"})
    if is_error(d):
        errored = True
        errors.append(str(d.get("_error"))[:120])
        d = {}
    seen = set()
    for i in (d or {}).get("items") or []:
        nm = i.get("name") or ""
        if not nm or nm in seen:
            continue
        seen.add(nm)
        body = skill_body(nm)
        txt = f"[{nm}] {body}" if body else f"{nm}: {i.get('description', '')}"
        if txt.strip():
            hits.append(txt[:900])
            meta.append({"name": nm, "chars": len(txt[:900])})
    d2 = call("/v3/atomic/search", {"query": query, "limit": limit,
                                    "top_k": limit, "scope": "team"})
    if is_error(d2):
        errored = True
        errors.append(str(d2.get("_error"))[:120])
        d2 = {}
    for x in (d2 or {}).get("items") or []:
        c = (x.get("content") or "")[:600]
        if c.strip():
            hits.append(c)
            meta.append({"sha8": content_sha8(c), "chars": len(c)})
    status = "fired" if hits else ("error" if errored else "empty")
    return {"hits": hits, "status": status, "meta": meta,
            "error": "; ".join(errors) or None}


# ----------------------------------------------------------------- write path
def seed_skill(name, content):
    """Write one lesson as a Skill asset. True = accepted, False = not (D1/D2).

    team_id+agent_id+user_id must be in the body — headers alone default the
    agent to 'default' and the hub answers 50001 agent not found; `call()`
    guarantees all three. A duplicate name is the hub's idempotent rejection
    (42201): False, never an exception, so a batch keeps going.
    """
    return not is_error(seed_result(name, content))


def seed_result(name, content):
    """The raw hub reply for one lesson write (so callers can classify it)."""
    if not content.lstrip().startswith("---"):
        desc = (content.splitlines() or ["teamkit lesson"])[0][:120]
        content = f"---\nname: {name}\ndescription: {desc}\n---\n{content}"
    return call("/v3/skill/create", {"name": name, "content": content,
                                     "task_id": TASK})


def seed_lessons(workspace, notes):
    """D1 batch write for a run. -> {"names","seeded","duplicates","errors","of"}.

    Names come from the lesson CONTENT, so one run holding three notes lands
    three distinct records, and a later run holding one of those same notes
    counts it as a duplicate rather than failing quietly.
    """
    names, seeded, dups, errs = [], 0, 0, 0
    for n in notes or []:
        nm = lesson_name(workspace, n)
        names.append(nm)
        res = seed_result(nm, n)
        if is_error(res):
            if is_duplicate(res):
                dups += 1
            else:
                errs += 1
        else:
            seeded += 1
    return {"names": names, "seeded": seeded, "duplicates": dups,
            "errors": errs, "of": len(notes or [])}


if __name__ == "__main__":
    print(json.dumps({"available": available(),
                      "gateway_health": __import__('urllib.request', fromlist=['urlopen']).urlopen(GATEWAY + '/health', timeout=5).status}))
