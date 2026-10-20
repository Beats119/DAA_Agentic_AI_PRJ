"""Deterministic built-in static checks (tool id ``"builtin"``).

These are deliberately simple, explainable pattern checks over C source —
NOT a replacement for cppcheck. When cppcheck is installed, the analyzer
additionally runs it and merges findings (declared as tool ``"cppcheck"``);
when it is missing, nothing is fabricated.

Each check yields ``(check_id, severity, line, message)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class StaticFinding:
    check_id: str
    severity: str  # "high" | "medium" | "low"
    line: int
    message: str
    tool: str = "builtin"


# Dangerous / discouraged function calls: (pattern, check_id, severity, advice)
_FUNC_CHECKS: list[tuple[re.Pattern, str, str, str]] = [
    (
        re.compile(r"\bgets\s*\("),
        "builtin.dangerous-gets",
        "high",
        "gets() cannot prevent buffer overflow — use fgets() with an explicit size",
    ),
    (
        re.compile(r"\bstrcpy\s*\("),
        "builtin.unbounded-strcpy",
        "medium",
        "strcpy() is unbounded — prefer strncpy()/strlcpy() or check lengths first",
    ),
    (
        re.compile(r"\bstrcat\s*\("),
        "builtin.unbounded-strcat",
        "medium",
        "strcat() is unbounded — prefer strncat()/strlcat() or check lengths first",
    ),
    (
        re.compile(r"\bsprintf\s*\("),
        "builtin.unbounded-sprintf",
        "medium",
        "sprintf() is unbounded — prefer snprintf() with an explicit size",
    ),
    (
        re.compile(r"\bsystem\s*\("),
        "builtin.system-call",
        "medium",
        "system() executes a shell command — avoid in student code; prefer exec-family",
    ),
]

_SCANF_NO_WIDTH = re.compile(r'\bscanf\s*\(\s*"([^"]*)"')
_WIDTH_SPEC = re.compile(r"%\d+")


def _strip_comments_and_strings(line: str) -> str:
    """Best-effort: remove // comments and string literals for matching."""
    line = line.split("//", 1)[0]
    line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
    line = re.sub(r"'(?:[^'\\]|\\.)*'", "''", line)
    return line


def run_builtin_checks(source: str) -> list[StaticFinding]:
    """Run all built-in checks over ``source``. Pure function, no I/O."""
    findings: list[StaticFinding] = []
    lines = source.splitlines()
    for lineno, raw in enumerate(lines, start=1):
        no_comment = raw.split("//", 1)[0]
        code = _strip_comments_and_strings(raw)
        for pattern, check_id, severity, advice in _FUNC_CHECKS:
            if pattern.search(code):
                findings.append(
                    StaticFinding(
                        check_id=check_id,
                        severity=severity,
                        line=lineno,
                        message=f"{check_id.split('.', 1)[1]}: {advice}",
                    )
                )
        # NOTE: the scanf format string must be inspected BEFORE string
        # literals are stripped (only // comments are removed here).
        m = _SCANF_NO_WIDTH.search(no_comment)
        if m and "%s" in m.group(1) and not _WIDTH_SPEC.search(m.group(1)):
            findings.append(
                StaticFinding(
                    check_id="builtin.scanf-no-width",
                    severity="low",
                    line=lineno,
                    message="scanf() with %s and no field width can overflow — use e.g. %255s",
                )
            )

    # Basic hygiene: non-void main without any return statement.
    main_match = re.search(r"\bint\s+main\s*\(", source)
    if main_match and not re.search(r"\breturn\b", source[main_match.end():]):
        # Find the line of main for the finding.
        main_line = source[: main_match.start()].count("\n") + 1
        findings.append(
            StaticFinding(
                check_id="builtin.main-no-return",
                severity="low",
                line=main_line,
                message="int main() has no return statement — add 'return 0;'",
            )
        )
    return findings


SEVERITY_PENALTY = {"high": 0.3, "medium": 0.15, "low": 0.05}

# Ids of every built-in check `run_builtin_checks` executes (all of them
# run on every source; findings are only produced on a match). The
# analyzer reports these in `checks_run` so a result states exactly
# which checks were performed.
BUILTIN_CHECK_IDS: list[str] = [cid for _, cid, _, _ in _FUNC_CHECKS] + [
    "builtin.scanf-no-width",
    "builtin.main-no-return",
]
