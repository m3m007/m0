"""Tests for lib/ledger — the single chokepoint that writes the quota ledger.

These exist because this repo's own test suite once wrote ~30 rows of fictional
request counts into ~/.hermes/quota/calls.jsonl: tests that made no API call
claimed they had, corrupting the owner's real budget numbers. The fix is one
gate (MULTIAGENT_LEDGER) plus these tests, which check BOTH directions:

  * off  -> nothing is written (so the suite cannot pollute the ledger)
  * on   -> something IS written (so 'off' is not just a broken pipe that makes
            every accounting claim vacuously true)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from lib import ledger  # noqa: E402

REAL_LEDGER = Path.home() / ".hermes" / "quota" / "calls.jsonl"


def _snapshot() -> str:
    return REAL_LEDGER.read_text(encoding="utf-8") if REAL_LEDGER.is_file() else ""


# --------------------------------------------------------------- the gate
def test_conftest_disables_the_ledger_for_the_suite():
    """If this fails, the suite is writing to the owner's real ledger again."""
    assert ledger.ledger_enabled() is False, (
        "MULTIAGENT_LEDGER must be off inside the test suite; tests/conftest.py "
        "sets it. Without this, tests fabricate quota usage.")


def test_record_is_a_noop_when_disabled():
    before = _snapshot()
    assert ledger.record("test/model", 5, "multiagent-stack:test") is False
    assert _snapshot() == before, "a disabled ledger must not be touched"


def test_record_disabled_for_zero_and_negative(monkeypatch):
    monkeypatch.setenv(ledger.ENV_FLAG, "on")
    assert ledger.record("test/model", 0) is False
    assert ledger.record("test/model", -3) is False


@pytest.mark.parametrize("value", ["off", "0", "no", "false", "disabled", "OFF"])
def test_disable_spellings(monkeypatch, value):
    monkeypatch.setenv(ledger.ENV_FLAG, value)
    assert ledger.ledger_enabled() is False


@pytest.mark.parametrize("value", ["on", "yes", "", "anything"])
def test_enable_is_the_default(monkeypatch, value):
    if value:
        monkeypatch.setenv(ledger.ENV_FLAG, value)
    else:
        monkeypatch.delenv(ledger.ENV_FLAG, raising=False)
    assert ledger.ledger_enabled() is True


def test_missing_env_means_recording(monkeypatch):
    """Production must record by default; opting out is the test's job."""
    monkeypatch.delenv(ledger.ENV_FLAG, raising=False)
    assert ledger.ledger_enabled() is True


# ------------------------------------------------------------ it really writes
def test_record_writes_a_row_when_enabled(tmp_path, monkeypatch):
    """Prove the pipe works, so 'off' cannot be hiding a permanently broken write."""
    calls = tmp_path / "calls.jsonl"
    fake_module = tmp_path / "tokenwatch.py"
    fake_module.write_text(
        "import json\n"
        f"CALLS = {str(calls)!r}\n"
        "def record(source, model, calls=1):\n"
        "    with open(CALLS, 'a') as fh:\n"
        "        fh.write(json.dumps({'source': source, 'model': model,\n"
        "                             'calls': calls}) + '\\n')\n",
        encoding="utf-8")
    monkeypatch.setenv(ledger.ENV_FLAG, "on")
    monkeypatch.setattr(ledger, "TOKENWATCH", fake_module)

    assert ledger.record("anthropic/glm-5.3", 2, "multiagent-stack:t") is True
    rows = [json.loads(line) for line in calls.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["model"] == "anthropic/glm-5.3"
    assert rows[0]["calls"] == 2
    assert rows[0]["source"].startswith("multiagent-stack")


def test_source_is_always_prefixed(tmp_path, monkeypatch):
    """Foreign sources must not slip in unlabelled: the ledger is shared."""
    calls = tmp_path / "calls.jsonl"
    fake = tmp_path / "tokenwatch.py"
    fake.write_text(
        "import json\n"
        f"CALLS = {str(calls)!r}\n"
        "def record(source, model, calls=1):\n"
        f"    open(CALLS,'a').write(json.dumps({{'source': source}})+'\\n')\n",
        encoding="utf-8")
    monkeypatch.setenv(ledger.ENV_FLAG, "on")
    monkeypatch.setattr(ledger, "TOKENWATCH", fake)

    ledger.record("m", 1, "someone-elses-source")
    written = json.loads(calls.read_text().splitlines()[0])["source"]
    assert written.startswith("multiagent-stack:"), written


def test_a_broken_tokenwatch_is_reported_not_fatal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ledger.ENV_FLAG, "on")
    monkeypatch.setattr(ledger, "TOKENWATCH", tmp_path / "does-not-exist.py")
    assert ledger.record("m", 1) is False          # no exception escapes
    assert "record failed" in capsys.readouterr().err


# ------------------------------------------------- router/repro use the gate
def test_router_record_does_not_touch_the_real_ledger():
    """The exact regression this file was written for."""
    from router import Router

    before = _snapshot()
    router = Router.load()
    with router.tracked("architect"):
        pass
    router.record("architect", 99)
    assert _snapshot() == before, "a unit test wrote to the owner's quota ledger"


def test_probe_budget_totals_are_internally_consistent():
    """The CLI's totals must agree with the ledger it reads, and the offline
    suite must add nothing to it.

    An earlier version of this test asserted "no router: rows exist". That went
    stale the moment live tests began recording legitimately: a
    multiagent-stack:router:* row now means a real request was spent, which is
    correct behaviour, not pollution. What must actually hold is that THIS
    offline suite contributes zero rows.
    """
    from lib.ledger import record
    from tests.probe_budget import by_model, rows, total

    assert total() == sum(by_model().values())
    assert total() == sum(int(r.get("calls", 1)) for r in rows())
    before_snapshot, before_total = _snapshot(), total()
    record("some/model", 5000, "multiagent-stack:would-be-a-lie")
    assert _snapshot() == before_snapshot, "offline record() reached the real ledger"
    assert total() == before_total


def test_suite_does_not_write_the_ledger_end_to_end():
    """Run a test that calls record() and prove nothing landed.

    Subprocess, because conftest's env must be applied the way pytest applies it.
    """
    script = tmp_script = REPO / "runs" / "_ledger_guard.py"
    script.parent.mkdir(exist_ok=True)
    tmp_script.write_text(
        "import sys; sys.path.insert(0, %r)\n"
        "import os; os.environ['MULTIAGENT_LEDGER']='off'\n"
        "from lib.ledger import record, ledger_enabled\n"
        "assert ledger_enabled() is False\n"
        "record('test/x', 1000, 'multiagent-stack:guard')\n"
        "print('OK')\n" % str(REPO), encoding="utf-8")
    before = _snapshot()
    proc = subprocess.run([sys.executable, str(tmp_script)], capture_output=True,
                          text=True, check=False)
    tmp_script.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _snapshot() == before, "1000 phantom requests were recorded"


# ------------------------------------------------ per-run vs cumulative truth
def test_cumulative_total_can_exceed_a_per_run_ceiling(tmp_path, monkeypatch):
    """The accounting trap this repo actually hit.

    router.yaml's ceiling caps ONE team run; the ledger holds every run. After a
    few live smoke runs the cumulative total passes the per-run number while no
    individual run ever did -- so a report that compares those two shouts
    "budget blown" when nothing is wrong. probe_budget must therefore expose both
    numbers separately and judge the ceiling against a time window, not the
    whole session.
    """
    import json as _json
    import time as _time

    ledger_path = tmp_path / "calls.jsonl"
    now = _time.time()
    # newest row at `now`, oldest at now-100, so a 50s window catches ~half
    rows_out = [{"ts": now - i * 20, "source": "multiagent-stack:repro",
                 "model": "openai/qwen3.8-flash", "calls": 40} for i in range(6)]
    ledger_path.write_text("".join(_json.dumps(r) + "\n" for r in rows_out))
    monkeypatch.setattr("tests.probe_budget.LEDGER", ledger_path)

    from tests import probe_budget

    assert probe_budget.total() == 240, "cumulative must count every row"
    recent = [r for r in probe_budget.rows() if r["ts"] >= now - 50]
    window = sum(r["calls"] for r in recent)
    assert 0 < window < 240, \
        f"a time window must report strictly less than the total, got {window}"


def test_probe_budget_cli_separates_window_from_cumulative(tmp_path, monkeypatch,
                                                           capsys):
    """The CLI must print BOTH numbers, and judge the ceiling against the window."""
    import json as _json
    import time as _time

    ledger_path = tmp_path / "calls.jsonl"
    now = _time.time()
    # five rows of 30, spread over ~50s: a 25s window sees a strict subset
    ledger_path.write_text("".join(_json.dumps(
        {"ts": now - i * 10, "source": "multiagent-stack:repro",
         "model": "m", "calls": 30}) + "\n" for i in range(5)))
    monkeypatch.setattr("tests.probe_budget.LEDGER", ledger_path)

    from tests import probe_budget

    assert probe_budget.main(["--since", str(now - 25), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["our_recorded_requests_cumulative"] == 150
    assert 0 < report["spent_since"] < 150, "window must be a strict subset"
    assert "within_ceiling" in report and "per_run_ceiling" in report
