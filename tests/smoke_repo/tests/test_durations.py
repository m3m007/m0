"""Gate for Task A. These FAIL against the shipped buggy durations.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from pkg.durations import humanise


def test_full_duration():
    assert humanise(3661) == "1h1m1s"


def test_zero_components_are_dropped():
    assert humanise(7200) == "2h"
    assert humanise(90) == "1m30s"
    assert humanise(120) == "2m"


def test_zero_is_the_one_exception():
    assert humanise(0) == "0s"


def test_seconds_only():
    assert humanise(45) == "45s"


def test_negative_rejected():
    with pytest.raises(ValueError):
        humanise(-1)
