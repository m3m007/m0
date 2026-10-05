"""Pytest setup for this repo.

MULTIAGENT_LEDGER=off — the important line in this file.

The quota ledger at ~/.hermes/quota/calls.jsonl is a real, shared accounting
file. An earlier pass of this test suite called Router.tracked()/record() and
wrote roughly thirty rows of FICTIONAL request counts into it: tests that never
made an API call were reporting that they had. That corrupts the owner's view of
their own quota, and it is invisible unless someone looks.

So the whole suite disables ledger writing at import time, before any test can
run. Production code still records (the flag defaults to on); only tests opt out.
tests/test_ledger.py covers the mechanism itself by re-enabling it against a
temporary file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Must be set before anything imports lib.ledger or calls record().
os.environ["MULTIAGENT_LEDGER"] = "off"
