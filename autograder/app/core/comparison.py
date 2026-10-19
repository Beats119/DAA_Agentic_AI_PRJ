"""Program output comparison under the rules' comparison policy.

Pure, deterministic, no I/O. The policy comes from
``RuleSet.comparison_policy``; analyzers must not invent their own.
"""

from __future__ import annotations

from .schemas import ComparisonPolicy


def compare_output(actual: str, expected: str, policy: ComparisonPolicy) -> bool:
    """Return True iff ``actual`` matches ``expected`` under ``policy``.

    - ``whitespace="exact"``: byte-identical match (after optional case fold).
    - ``whitespace="normalize_trailing"``: trailing whitespace stripped from
      each line before comparison (interior spacing still exact).
    - ``case_sensitive=False``: both sides case-folded before comparison.
    """
    a, e = actual, expected
    if not policy.case_sensitive:
        a, e = a.casefold(), e.casefold()
    if policy.whitespace == "normalize_trailing":
        a = _normalize_trailing(a)
        e = _normalize_trailing(e)
    elif policy.whitespace != "exact":
        raise ValueError(f"unknown whitespace policy: {policy.whitespace!r}")
    return a == e


def _normalize_trailing(s: str) -> str:
    """Strip trailing whitespace from each line and drop trailing empty
    lines, so a final newline is not content (``"5\\n"`` equals ``"5"``).
    Interior spacing stays exact."""
    lines = [line.rstrip() for line in s.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)
