"""Unit tests: output comparison policies."""

from __future__ import annotations

import pytest

from autograder.app.core.comparison import compare_output
from autograder.app.core.schemas import ComparisonPolicy


def pol(**kw):
    return ComparisonPolicy(**kw)


def test_exact_match():
    assert compare_output("5\n", "5\n", pol()) is True


def test_exact_mismatch_whitespace():
    assert compare_output("5 \n", "5\n", pol()) is False


def test_normalize_trailing():
    p = pol(whitespace="normalize_trailing")
    assert compare_output("5   \n3\t\n", "5\n3\n", p) is True
    # interior spacing still exact
    assert compare_output("a  b\n", "a b\n", p) is False


def test_case_insensitive():
    p = pol(case_sensitive=False)
    assert compare_output("PASS\n", "pass\n", p) is True
    assert compare_output("PASS\n", "pass\n", pol()) is False


def test_empty_strings():
    assert compare_output("", "", pol()) is True
    assert compare_output("", "x", pol()) is False


def test_unknown_policy_rejected():
    with pytest.raises(Exception):
        compare_output("a", "a", pol(whitespace="fuzzy"))
