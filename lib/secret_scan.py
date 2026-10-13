#!/usr/bin/env python3
"""Pre-commit secret gate for this repo.

Run as ``python3 lib/secret_scan.py`` (or ``bash .secrets-safe.sh``).
Exit 0 = clean. Exit 1 = something credential-shaped is in the tree, and the
only thing printed is ``FILE:LINE [rule]`` -- never the offending value, so the
scanner cannot itself become the leak it exists to prevent.

Why hash-compare instead of grep: ``grep -F "$REAL_KEY" .`` puts the real key on
a process argument list (visible in ``ps``) and into shell history. Instead we
hash the live key obtained through :mod:`lib.keys` and compare it against
hashes of the credential-shaped tokens found in files. A file holding the
owner's actual key is flagged without that value ever being echoed, exec'd, or
written.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
             ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".so", ".zip", ".gz", ".png", ".jpg",
                 ".gif", ".pdf", ".whl", ".db", ".sqlite"}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # A provider secret key has digits in it; an English word after "sk-" does
    # not. Requiring >=1 digit in the body keeps prose ("sk-provider-key") and
    # rule names out of the findings without weakening real detection.
    ("provider-key-shaped", re.compile(r"\bsk-(?=[A-Za-z0-9._\-]*[0-9])[A-Za-z0-9][A-Za-z0-9._\-]{8,}\b")),
    ("bearer-token", re.compile(r"(?i)\b(?:bearer|basic)\s+(?=[A-Za-z0-9._\-+/]*[0-9])[A-Za-z0-9._\-+/]{20,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRI" + r"VATE KEY-----")),
    ("jwt-like", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{4,}")),
    # A credential-named key assigned a literal long value. ``{`` and ``}`` are
    # excluded from the value class so shell/f-string templates
    # (``${BAILIAN_TOKEN_PLAN_API_KEY}``, ``{decoy_key()}``) are recognised as
    # *references* -- which is exactly the pattern this repo mandates.
    ("kv-assignment", re.compile(
        r"(?i)\b(?:api[_\-]?key|authorization|x-api-key|access[_\-]?token|secret[_\-]?key)"
        r"""["']?\s*[:=]\s*["']?([A-Za-z0-9._\-+/]{12,})""")),
]

#: The subset of the shape rules used to mask secrets inside captured text
#: (e.g. raw HTTP ``set-cookie`` headers, which have no ``key:`` prefix to hang
#: a name-based rule on). Deliberately strict: alphanumeric body, 9+ chars.
CREDENTIAL_SHAPE_RE = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._\-]{8,}\b")

# Deliberate placeholders used in tests and docs. These are only forgiven when
# they carry an unmistakable tell; a real key is never forgiven, because the
# live-key substring test in scan_line() runs BEFORE this list is consulted.
ALLOW_EXACT = {"<REDACTED>", "REPLACE_ME", "changeme", "sk-FAKE"}
# ``env:`` covers documentation that names a variable instead of a value,
# e.g. ``api_key: ${BAILIAN_TOKEN_PLAN_API_KEY}`` -- a reference, not a secret.
ALLOW_HINTS = ("fake", "example", "placeholder", "redacted", "dummy",
               "not-real", "not-a-real", "xxxxxxxx", "deadbeef", "replace",
               "env:", "your-", "yyy", "zzz", "<")
# NOTE: there is deliberately NO hint that an ordinary random key body could
# accidentally contain (no "1234", no "abcdef", no single letters). Test decoys
# stay out of the committed source by being assembled at runtime from fragments
# -- see tests/test_keys.py::decoy_key -- not by teaching the scanner to ignore
# a shape that a real key might share.


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def live_keys() -> dict[str, str]:
    """``label -> value`` for every key this machine can actually use.

    Held only in process memory, never written or printed. Used for a plain
    substring test, which (unlike a regex) cannot be fooled by formatting.
    """
    found: dict[str, str] = {}
    sys.path.insert(0, str(REPO_ROOT))
    try:
        from lib import keys  # noqa: PLC0415
    except Exception:  # pragma: no cover - broken install, not a finding
        return found
    for label, getter in (("token_plan", keys.token_plan_key), ("kimi", keys.kimi_key)):
        try:
            value = getter()
            if value and len(value) >= 8:
                found[label] = value.strip()
        except Exception:
            continue
    return found


def live_key_hashes() -> dict[str, str]:
    """``hash -> label`` for the live keys (kept for API compatibility)."""
    return {_sha(v): label for label, v in live_keys().items()}


def _real_key_hit(line: str, live: dict[str, str]) -> str | None:
    """Return the label of a live key present in ``line``.

    Catches the whole key, and also any 24+ character slice of it -- enough to
    recognise a truncated-in-a-log leak without tripping on the 4-character
    hint that :func:`lib.keys.describe` deliberately prints.
    """
    for label, value in live.items():
        if len(value) < 8:
            continue
        if value in line:
            return label
        window = min(24, len(value))
        for start in range(0, len(value) - window + 1):
            if value[start:start + window] in line:
                return label
    return None


def iter_text_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            path = here / name
            if path.resolve() == SELF:
                continue  # this file legitimately carries the regexes
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            try:
                if path.stat().st_size > 4_000_000:
                    continue
                with open(path, "rb") as handle:
                    if BINARY_SNIFF in handle.read(2048):
                        continue
            except OSError:
                continue
            yield path


BINARY_SNIFF = b"\x00"


def looks_allowable(token: str) -> bool:
    if token in ALLOW_EXACT:
        return True
    lowered = token.lower()
    return any(hint in lowered for hint in ALLOW_HINTS)


def scan_line(line: str, live: dict[str, str] | None = None) -> str | None:
    """Return the rule name if this line leaks, else ``None``.

    Order matters: the live-key substring test runs FIRST, so a real key can
    never be forgiven by the allowlist or by the callable exception below.
    """
    live = live or {}
    hit = _real_key_hit(line, live)
    if hit:
        return f"real-key:{hit}"
    for rule, pattern in PATTERNS:
        for match in pattern.finditer(line):
            token = (match.group(1) if match.groups() else match.group(0)) or ""
            token = token.strip("\"'`,;)}")
            if not token:
                continue
            # ``api_key=<name>(`` is a *reference* -- a function call resolving
            # the credential at runtime, which is exactly the pattern lib/keys.py
            # mandates. Only the name-based rule relaxes for it: a literal secret
            # is still caught above (live-key) or by the shape rules, none of
            # which can match a bare identifier followed by "(".
            if rule == "kv-assignment":
                rest = line[match.end():]
                if rest.lstrip()[:1] == "(":
                    continue
            if looks_allowable(token):
                continue
            return rule
    return None


def scan_file(path: Path, root: Path, live: dict[str, str] | None = None) -> list[str]:
    findings: list[str] = []
    rel = path.relative_to(root) if path.is_relative_to(root) else path
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                rule = scan_line(line.rstrip("\n"), live)
                if rule:
                    findings.append(f"{rel}:{number} [{rule}]")
    except OSError as exc:  # pragma: no cover
        findings.append(f"{rel}:0 [unreadable:{type(exc).__name__}]")
    return findings


def scan_git_history(live: dict[str, str] | None = None,
                     root: Path | None = None) -> list[str]:
    """Scan every commit's diff, not just the working tree.

    Why this exists separately: a secret committed and then deleted three commits
    later is invisible to a working-tree scan and perfectly visible to anyone who
    clones the repo. The brief's constraint is "no secrets in files/git/logs", and
    git means history. Findings are attributed to the file the diff touches.

    ``root`` must be honoured: an earlier version hardcoded this module's own repo,
    so scanning any other path silently reported "history clean" while reading the
    wrong repository -- a vacuous pass of exactly the kind the negative-control
    tests exist to prevent.
    """
    import subprocess

    target = Path(root or REPO_ROOT)
    try:
        proc = subprocess.run(["git", "log", "-p", "--all"], cwd=str(target),
                              capture_output=True, text=True, check=False)
    except OSError as exc:
        return [f"<git>:0 [unavailable:{type(exc).__name__}]"]
    if proc.returncode != 0:
        return [f"<git>:0 [git-log-failed:{proc.stderr.strip()[:60]}]"]

    findings: list[str] = []
    current, line_no = "<header>", 0
    for line in (proc.stdout or "").splitlines():
        if line.startswith("commit "):
            current = "<commit:" + line.split()[1][:10] + ">"
            line_no = 0
        elif line.startswith("diff --git "):
            current = line.split(" b/")[-1] if " b/" in line else current
            line_no = 0
        line_no += 1
        rule = scan_line(line, live)
        if rule:
            findings.append(f"{current}:{line_no} [history:{rule}]")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a tree for credential-shaped strings.")
    parser.add_argument("paths", nargs="*", default=None,
                        help="files/dirs to scan (default: repo root)")
    parser.add_argument("--verbose", action="store_true",
                        help="print the clean-file count too")
    parser.add_argument("--history", action="store_true",
                        help="also scan every commit's diff in git history")
    args = parser.parse_args(argv)

    root = REPO_ROOT
    live = live_keys()
    targets = [Path(p) for p in (args.paths or [root])]

    findings: list[str] = []
    scanned = 0
    for target in targets:
        if target == Path("/dev/stdin"):
            # accept piped input (e.g. `git log -p | ... /dev/stdin`)
            data = sys.stdin.read()
            for number, line in enumerate(data.splitlines(), 1):
                rule = scan_line(line, live)
                if rule:
                    findings.append(f"<stdin>:{number} [{rule}]")
            scanned += 1
            continue
        if target.is_dir():
            for path in iter_text_files(target):
                scanned += 1
                findings.extend(scan_file(path, root, live))
        elif target.is_file():
            scanned += 1
            findings.extend(scan_file(target, root, live))
        else:
            findings.append(f"{target}:0 [missing]")

    if args.history:
        for hist_root in {str(Path(t).resolve().parent if Path(t).is_file()
                             else Path(t).resolve()) for t in (args.paths or [root])}:
            findings.extend(scan_git_history(live, Path(hist_root)))

    if findings:
        print(f"SECRET SCAN FAILED: {len(findings)} finding(s) in {scanned} file(s)")
        for finding in findings:
            print(f"  {finding}")
        print("Fix: reference the key by env-var NAME (lib/keys.py) or scrub "
              "captured output with lib.keys.redact().")
        return 1
    if args.verbose:
        print(f"secret scan OK: {scanned} file(s) clean, "
              f"{len(live)} live key(s) known for substring comparison"
              + (", git history clean" if args.history else ""))
    else:
        print(f"secret scan OK: {scanned} file(s) clean"
              + (" + git history" if args.history else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
