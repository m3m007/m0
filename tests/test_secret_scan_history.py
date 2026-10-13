"""Tests for the git-history half of the secret scanner.

The brief's constraint is "no secrets in files/git/logs". A working-tree scan
satisfies "files"; it says nothing at all about "git", because a secret that was
committed and then deleted three commits later is invisible to the tree and
plainly visible to anyone who clones the repo.

These tests exist because the first version of scan_git_history() hardcoded
this module's own repository as the git cwd. Scanning any *other* path therefore
reported "history clean" while reading the wrong repo entirely -- a green result
that meant nothing. That is precisely the vacuous-pass failure mode the rest of
this suite is built to catch, so it gets an explicit negative control here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lib import secret_scan  # noqa: E402

# Assembled at runtime so this file commits no credential-shaped literal.
PREFIX = "sk-"
BODY = "TESTVAL" + "9Qz7W" + "erp4Xk2Rv"
SECRET = PREFIX + BODY


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def make_repo_with_deleted_secret(tmp_path: Path) -> Path:
    """Commit a file holding a secret, then delete the file. Tree looks clean."""
    leak = tmp_path / "leak.py"
    leak.write_text('api_key = "' + SECRET + '"\n', encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qm", "plant a secret")
    leak.unlink()
    _git(tmp_path, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qam", "delete the secret again")
    return tmp_path


# --------------------------------------------------------- the tree misses it
def test_working_tree_scan_misses_the_deleted_secret(tmp_path):
    """Baseline: this is the gap the history scan has to close."""
    repo = make_repo_with_deleted_secret(tmp_path)
    files = [p for p in repo.iterdir() if p.is_file() and p.name != ".git"]
    findings: list[str] = []
    for path in files:
        findings.extend(secret_scan.scan_file(path, repo, {}))
    assert not findings, "the tree should look clean (that is the point)"


# ------------------------------------------------------ history catches it
def test_history_scan_finds_the_deleted_secret(tmp_path):
    repo = make_repo_with_deleted_secret(tmp_path)
    findings = secret_scan.scan_git_history({}, repo)
    assert findings, "history scan found nothing in a repo with a deleted secret"
    assert any("[history:" in f for f in findings), findings[:3]
    # attributed to the offending file, not a bare blob
    assert any("leak.py" in f for f in findings), findings[:3]


def test_history_scan_never_echoes_the_secret(tmp_path):
    """Findings carry FILE:LINE and a rule name; the value must never appear."""
    repo = make_repo_with_deleted_secret(tmp_path)
    joined = " ".join(secret_scan.scan_git_history({}, repo))
    assert SECRET not in joined
    assert BODY not in joined, "a fragment of the secret leaked into the report"


def test_history_scan_honours_its_root_argument(tmp_path):
    """Regression: an earlier version always read this module's own repo.

    Two sibling repos, only one of which holds a secret. If `root` is ignored the
    clean one reports the dirty one's findings (or reports nothing at all) and a
    caller would draw exactly the wrong conclusion.
    """
    repo = make_repo_with_deleted_secret(tmp_path)
    elsewhere = tmp_path / "second"
    elsewhere.mkdir()
    _git(elsewhere, "init", "-q")
    _git(elsewhere, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "nothing here")

    dirty = secret_scan.scan_git_history({}, repo)
    clean = secret_scan.scan_git_history({}, elsewhere)
    assert dirty, "the repo that DOES hold a secret reported clean"
    assert not any("leak.py" in f for f in clean), \
        f"the clean repo reported the other repo's findings: {clean}"


def test_non_repo_is_reported_not_silently_clean(tmp_path):
    """A missing git repo must be a finding, never a clean bill of health."""
    empty = tmp_path / "nogit"
    empty.mkdir()
    findings = secret_scan.scan_git_history({}, empty)
    assert findings, "a directory with no git repo scanned 'clean'"
    assert any("git-log-failed" in f or "unavailable" in f for f in findings), findings


def test_allowlist_still_applies_inside_history(tmp_path):
    """Docs and test fixtures contain fake keys; history must not scream."""
    repo = tmp_path / "docs"
    repo.mkdir()
    (repo / "README.md").write_text(
        "Set api_key to ${BAILIAN_TOKEN_PLAN_API_KEY}; a real one looks like\n"
        "sk-FAKEEXAMPLE0000 and is always shown as <REDACTED>.\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         "commit", "-qm", "docs")
    assert secret_scan.scan_git_history({}, repo) == []


# ------------------------------------------------------- the claim for us
def test_this_repos_history_is_clean():
    """The assertion that actually matters, run the way the tool is run."""
    proc = subprocess.run([sys.executable, str(REPO / "lib" / "secret_scan.py"),
                           "--history"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "git history" in proc.stdout
