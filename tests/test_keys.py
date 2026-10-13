"""Offline tests for the secret-safety layer (task T0 gate). No network, no quota.

DESIGN RULE: this file must pass the repo's own secret scanner. So it contains
no complete credential literal -- decoys are assembled at runtime from
fragments by :func:`decoy_key`. That is structural, not just behavioural: even
if this file were committed and then read by a human, it leaks nothing.

A real key is never used as a fixture either. Test files get committed, echoed
on failure, and pasted into bug reports.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lib import keys, secret_scan  # noqa: E402

# Fragments, split so that no single line of this source holds a whole token.
_P1 = "sk-"
_P2 = "QZ7Rb"
_P3 = "N3TdXkV"
_BLOB = "Zq8Wm" + "Lt5Hp" + "Rn2Yk" + "Gv9Xc"  # a prefix-less synthetic secret


def decoy_key() -> str:
    """Well-formed, has a digit (so the shape rule fires), and is unusable."""
    return _P1 + _P2 + _P3


def decoy_line() -> str:
    return "api_key: " + decoy_key()


# ---------------------------------------------------------------- keys.py
def test_base_url_returns_both_documented_endpoints():
    assert keys.base_url("openai") == (
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1")
    assert keys.base_url("anthropic") == (
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic")
    assert keys.base_url("compatible-mode") == keys.COMPATIBLE_BASE_URL
    assert keys.base_url("anthropic-messages") == keys.ANTHROPIC_BASE_URL
    assert keys.base_url("kimi") == "https://api.kimi.com/coding/v1"


def test_base_url_accepts_the_prefix_convention():
    """The 'openai/glm-5.3' style prefix maps to the matching endpoint."""
    for prefix, expected in (("openai/glm-5.3", keys.COMPATIBLE_BASE_URL),
                             ("anthropic/glm-5.3", keys.ANTHROPIC_BASE_URL)):
        head = prefix.split("/", 1)[0]
        assert keys.base_url(head) == expected


def test_base_url_rejects_unknown_transport():
    with pytest.raises(ValueError):
        keys.base_url("groq")


def test_token_plan_key_resolves_non_empty():
    value = keys.token_plan_key()
    assert isinstance(value, str) and value.strip()


def test_key_missing_raises_a_message_that_names_no_value(monkeypatch):
    monkeypatch.delenv(keys.TOKEN_PLAN_ENV, raising=False)
    monkeypatch.setattr(keys, "HERMES_CONFIG", "/nonexistent/config.yaml")
    with pytest.raises(keys.TokenPlanKeyNotFound) as exc:
        keys.token_plan_key()
    assert keys.TOKEN_PLAN_ENV in str(exc.value)  # names the variable
    assert "sk-" not in str(exc.value)


def test_describe_leaks_nothing():
    info = keys.describe()
    real = keys.token_plan_key()
    joined = "".join(info.values())
    assert real not in joined, "describe() printed the key"
    assert info["token_plan_hint"].endswith("\u2026")
    assert len(info["token_plan_hint"]) <= 6, "hint longer than 4 chars + ellipsis"


def test_redact_catches_nested_key_three_levels_deep():
    payload = {"a": {"b": {"c": {"api_key": decoy_key(), "note": "hello"}}}}
    out = keys.redact(payload)
    assert out["a"]["b"]["c"]["api_key"] == keys.REPLACEMENT
    assert out["a"]["b"]["c"]["note"] == "hello", "over-redacted siblings"


def test_redact_scrubs_secret_embedded_in_free_text():
    text = "acompletion(api_key=%s, model='anthropic/glm-5.3')" % decoy_key()
    scrubbed = keys.redact(text)
    assert decoy_key() not in scrubbed
    assert keys.REPLACEMENT in scrubbed
    assert "anthropic/glm-5.3" in scrubbed, "over-redacted useful context"


def test_redact_does_not_mutate_input():
    original = {"api_key": decoy_key(), "list": [decoy_key()], "keep": 1}
    snapshot = copy.deepcopy(original)
    keys.redact(original)
    assert original == snapshot, "redact() mutated its argument"


def test_redact_handles_objects_tuples_and_cycles():
    class Box:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    out = keys.redact(Box(api_key=decoy_key(), model="glm-5.3"))
    assert out["api_key"] == keys.REPLACEMENT and out["model"] == "glm-5.3"
    assert decoy_key() not in keys.redact(("x", decoy_key()))
    node: dict = {"name": "root"}
    node["self"] = node
    assert keys.redact(node)["name"] == "root", "cycle handling broken"


def test_scrub_removes_the_owners_actual_live_key():
    """The strongest guarantee: exact-value scrubbing, not heuristic shape."""
    real = keys.token_plan_key()
    leaked = f"request failed using {real} against {keys.COMPATIBLE_BASE_URL}"
    scrubbed = keys.scrub_text(leaked)
    assert real not in scrubbed
    assert keys.REPLACEMENT in scrubbed
    assert keys.COMPATIBLE_BASE_URL in scrubbed, "endpoint must survive scrubbing"


def test_scrub_removes_truncated_live_key():
    """A key cut off in a log line is still caught (24-char sliding window)."""
    real = keys.token_plan_key()
    fragment = real[:26]
    assert fragment != real
    assert fragment not in keys.scrub_text(f"...{fragment}...")


# ------------------------------------------------------- secret_scan gate
def test_scanner_flags_planted_decoy_and_reports_only_file_line(tmp_path):
    decoy = tmp_path / "leaked.yml"
    decoy.write_text("top: 1\n" + decoy_line() + "\n", encoding="utf-8")
    findings = secret_scan.scan_file(decoy, tmp_path, {})
    assert len(findings) == 1, findings
    assert findings[0].endswith("[provider-key-shaped]")
    assert decoy.name in findings[0] and ":2" in findings[0]
    assert decoy_key() not in " ".join(findings), "scanner echoed the secret!"


def test_scanner_flags_bearer_blob_and_private_key_block():
    assert secret_scan.scan_line("auth = Bearer " + _BLOB + "7q", {}) == "bearer-token"
    assert secret_scan.scan_line("-----BEGIN RSA PRI" + "VATE KEY-----", {})


def test_scanner_detects_exact_live_key_in_arbitrary_format():
    """Prefix-less token: no shape rule can see it, only the live-key test."""
    live = {"kimi": _BLOB}
    assert secret_scan.scan_line("noise " + _BLOB + " noise", live) == "real-key:kimi"
    assert secret_scan.scan_line("noise " + _BLOB + " noise", {}) is None
    # and inside a Bearer line, the real-key label wins over the shape rule
    assert secret_scan.scan_line("Bearer " + _BLOB, live) == "real-key:kimi"


def test_scanner_real_key_beats_the_placeholder_allowlist():
    tricky = _P1 + "example" + "VALUE" + "0123456789"
    live = {"token_plan": tricky}
    # live-key check runs FIRST, so a real key containing "example" still fails
    assert secret_scan.scan_line(f"api_key: {tricky}", live) == "real-key:token_plan"
    # the identical string is forgiven when it is NOT a live key
    assert secret_scan.scan_line(f"api_key: {tricky}", {}) is None


def test_scanner_catches_truncated_real_key():
    # a real provider key is long; make the decoy comparable in length
    real = _P1 + "7Qz4W" + "erp8Xk2Rv" + "5Nm3Bd" + "6Tw8Yp" + "9Kz1Ra"
    live = {"token_plan": real}
    fragment = real[:28]
    assert len(fragment) < len(real), "fixture too short to be a truncation test"
    assert secret_scan.scan_line(f"trace ...{fragment}...", live) == "real-key:token_plan"


def test_scanner_ignores_env_references_and_redaction_markers():
    for benign in [
        "api_key: ${BAILIAN_TOKEN_PLAN_API_KEY}",
        "api_key: " + '"' + "${KIMI_API_KEY}" + '"',
        "api_key: <REDACTED>",
        "# set BAILIAN_TOKEN_PLAN_API_KEY in your shell",
        'kwargs = {"model": "anthropic/glm-5.3", "api_key": "<REDACTED>"}',
        "return f\"api_key: {decoy_key()}\"",
        "rule name: provider-key-shaped",
        # callable reference: resolves the key at runtime, holds no literal
        "api_key=token_plan_key(),",
        '"api_key": keys.token_plan_key(),',
        "llm = LLM(model=model, api_key=token_plan_key(), base_url=base_url)",
        "api_key  = keys.token_plan_key()   # env var, else config.yaml",
    ]:
        assert secret_scan.scan_line(benign, {}) is None, benign


def test_callable_exception_cannot_hide_a_literal_secret():
    """The 'name followed by (' carve-out must not become a loophole."""
    literal = _P1 + "9Qz7W" + "erp4Xk2" + "8Nm3Bd"
    # literal secret glued to a call: shape rule still fires
    assert secret_scan.scan_line(f"api_key={literal}token_plan_key()", {}) == \
        "provider-key-shaped"
    # a REAL key is caught regardless of what follows it
    live = {"token_plan": literal}
    assert secret_scan.scan_line(f"api_key = {literal} (", live) == "real-key:token_plan"
    # bare identifier with no parens is still treated as a literal assignment.
    # Built at runtime so this test file commits no credential-shaped literal.
    bare = "api_key = " + "token_plan_value_xyz7" + "89"
    assert secret_scan.scan_line(bare, {}) == "kv-assignment"


def test_scanner_passes_over_the_repo_as_committed():
    rc = subprocess.run([sys.executable, str(REPO / "lib" / "secret_scan.py"), str(REPO)],
                        capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stdout + rc.stderr


def test_this_test_file_contains_no_whole_credential_literal():
    here = Path(__file__).read_text(encoding="utf-8")
    hits = secret_scan.CREDENTIAL_SHAPE_RE.findall(here)
    assert not hits, f"complete sk- token written into test source: {hits}"
    assert decoy_key() not in here
    assert _BLOB not in here


def test_scanner_negative_control_is_not_vacuous(tmp_path):
    """Prove the gate can actually fail: inject a decoy, expect exit 1."""
    victim = tmp_path / "app.py"
    victim.write_text("API = '" + decoy_key() + "'\n", encoding="utf-8")
    rc = subprocess.run([sys.executable, str(REPO / "lib" / "secret_scan.py"), str(victim)],
                        capture_output=True, text=True, check=False)
    assert rc.returncode == 1, "secret gate passed a planted decoy -- vacuous!"
    assert decoy_key() not in rc.stdout, "gate printed the value it found"


def test_keys_demo_runs_without_printing_the_key():
    rc = subprocess.run([sys.executable, str(REPO / "lib" / "keys.py")],
                        capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stdout + rc.stderr
    assert keys.token_plan_key() not in rc.stdout, "demo printed the real key"

