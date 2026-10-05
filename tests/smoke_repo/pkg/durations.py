"""Duration rendering. INTENTIONALLY BUGGY — this is the smoke-test target.

Bug: zero components are always kept, so 3661 renders "1h1m1s" (right) but 7200
renders "2h0m0s" instead of "2h", and 0 renders "0h0m0s" instead of "0s".
"""

from __future__ import annotations

__all__ = ["humanise"]


def humanise(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 0:
        raise ValueError("negative durations are not supported")
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    # FIXME: drop zero components, except when the whole value is zero, in which
    # case the answer is "0s".
    return f"{hours}h{minutes}m{secs}s"
