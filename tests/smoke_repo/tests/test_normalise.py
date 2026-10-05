"""Gate for Task B. These FAIL against the shipped buggy normalise.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pkg.normalise import slugify


def test_basic():
    assert slugify("  Hello, World!  ") == "hello-world"


def test_runs_collapse():
    assert slugify("HTTP  Status--200") == "http-status-200"


def test_only_punctuation_is_empty():
    assert slugify("___") == ""
    assert slugify("") == ""


def test_already_clean():
    assert slugify("already-a-slug") == "already-a-slug"


def test_leading_and_trailing_stripped():
    assert slugify("!!urgent!!") == "urgent"
