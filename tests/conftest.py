"""Shared fixtures for all test suites (unit + integration)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "unit"))

from conftest import base_rules_dict, make_result, make_rules  # noqa: E402,F401

__all__ = ["base_rules_dict", "make_result", "make_rules"]
