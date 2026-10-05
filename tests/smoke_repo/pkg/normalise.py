"""Text normalisation. INTENTIONALLY BUGGY — this is the smoke-test target.

Bug: non-alphanumerics are replaced one-for-one instead of being collapsed, so
"Hello, World!" becomes "hello--world-" rather than "hello-world".
"""

from __future__ import annotations

__all__ = ["slugify"]


def slugify(text: str) -> str:
    out = []
    for ch in (text or "").lower():
        out.append(ch if ch.isalnum() else "-")
    # FIXME: collapse runs of "-" into one, and strip leading/trailing "-".
    return "".join(out)
