"""Repo-root pytest bootstrap: make the SDK importable, never skip the suite.

Why this file exists. The acceptance gate for this repo is
`/usr/bin/python3 -m pytest tests/ -q`. Bare system python3 carries pytest but
NOT openhands-sdk; the SDK lives in the shared venv at
~/.hermes/cache/scratch/oh-venv. Before this shim, ~40 pre-existing tests
(test_oh_compat, test_router, test_team_workflow, the offline e2e smoke) failed
with `ModuleNotFoundError: No module named 'openhands'` on the gate's own
interpreter — a harness gap, not a code defect, and it hid real regressions.

What it does, deliberately narrowly:
  * only if `openhands` cannot already be imported;
  * only appends the venv's site-packages, at the END of sys.path, so a real
    install always wins;
  * if the venv is missing, it stays silent — those tests fail the same way they
    did before, so nothing is masked.
No test opts out, and no test's semantics change: they simply get to import the
package they were written against.
"""

from __future__ import annotations

import sys
from pathlib import Path

SDK_SITE = Path("/home/hermes/.hermes/cache/scratch/oh-venv/lib/python3.14/"
                 "site-packages")

try:                                # already importable: touch nothing
    import openhands  # noqa: F401
except Exception:
    if SDK_SITE.is_dir() and str(SDK_SITE) not in sys.path:
        sys.path.append(str(SDK_SITE))
