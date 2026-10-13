"""lib.ledger — the ONE place that writes to the owner's quota ledger.

Why this exists as its own module: the ledger at ~/.hermes/quota/calls.jsonl is a
shared, real accounting file. When this repo's unit tests called tokenwatch
directly they wrote ~30 rows of FICTIONAL request counts into it, quietly
corrupting the numbers the owner uses to judge quota. Anything that counts
requests must go through here, and here alone, so one env flag controls it.

    MULTIAGENT_LEDGER=off   -> writes are dropped (what the test suite sets)
    unset / anything else    -> write the real ledger

Default is ON, because the honest behaviour for production code is to record;
the opt-out is what tests do, not what library code assumes. Redirection is not
offered: tokenwatch owns its own path, so this module only ever decides
write-or-don't.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REAL_LEDGER = Path.home() / ".hermes" / "quota" / "calls.jsonl"
TOKENWATCH = Path.home() / ".hermes" / "scripts" / "tokenwatch.py"
ENV_FLAG = "MULTIAGENT_LEDGER"
#: Optional redirect, for tests and dry runs. tokenwatch resolves its own path
#: at call time, so we patch the module attribute rather than editing anything.
ENV_LEDGER_PATH = "MULTIAGENT_LEDGER_PATH"
SOURCE_PREFIX = "multiagent-stack"

__all__ = ["SOURCE_PREFIX", "ledger_enabled", "record", "target_ledger"]


def target_ledger() -> Path:
    """Where record() will write. Overridable so accounting is testable."""
    override = (os.environ.get(ENV_LEDGER_PATH) or "").strip()
    return Path(override).expanduser() if override else REAL_LEDGER


def ledger_enabled() -> bool:
    """False only when MULTIAGENT_LEDGER=off/0/no. Unset means record."""
    value = (os.environ.get(ENV_FLAG) or "").strip().lower()
    return value not in {"off", "0", "no", "false", "disabled"}


def record(model: str, calls: int = 1, source: str = SOURCE_PREFIX) -> bool:
    """Append N requests to the ledger. Returns True if it actually wrote.

    Never raises: accounting must not be able to break a run. A failure to
    record is reported on stderr, because a silently missing count is how a
    budget report becomes fiction.
    """
    if calls <= 0:
        return False
    if not ledger_enabled():
        return False
    try:
        spec = importlib.util.spec_from_file_location("tokenwatch", TOKENWATCH)
        if spec is None or spec.loader is None:
            print(f"[ledger] tokenwatch not loadable at {TOKENWATCH}",
                  file=sys.stderr)
            return False
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not source.startswith(SOURCE_PREFIX):
            source = f"{SOURCE_PREFIX}:{source}"
        module.record(source, model, calls)
        return True
    except Exception as exc:  # pragma: no cover - best-effort by design
        print(f"[ledger] record failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
