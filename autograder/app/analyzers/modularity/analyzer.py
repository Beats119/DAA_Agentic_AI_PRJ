"""Modularity analyzer: function decomposition heuristics.

Best-effort regex-based extraction of top-level C function definitions.
Records ``parse_confidence`` (fraction of candidate definitions successfully
parsed with balanced braces). Scoring is threshold-based from
``metric.params``; findings name the offending functions.

``exclude_main_from_length`` (default True): ``main()`` is not flagged for
length — it is the entry point, not a decomposition failure. Documented here
so the policy is explicit, not hidden.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..base import AnalyzerContext, build_result

ANALYZER_ID = "modularity"

DEFAULTS = {
    "max_loc_per_function": 40,
    "min_functions": 2,
    "long_function_penalty": 0.15,
    "single_function_penalty": 0.2,
    "exclude_main_from_length": True,
    "score_floor": 0.0,
}

# Matches a likely top-level function definition header, e.g.
# "static int helper(int x)" followed by "{" on the same or next line.
_FUNC_RE = re.compile(
    r"""(?mx)
    ^(?P<ret>(?:static\s+|inline\s+|const\s+)*[A-Za-z_][\w\s\*]*?)
    \s+(?P<name>[A-Za-z_]\w*)
    \s*\([^;{}]*\)          # parameter list (no ; or braces -> not a prototype)
    \s*(?:\{|$)             # opening brace on this line, or next line
    """
)


def _strip_comments_strings(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    source = re.sub(r'"(?:[^"\\]|\\.)*"', '""', source)
    source = re.sub(r"'(?:[^'\\]|\\.)*'", "''", source)
    return source


def _extract_functions(source: str) -> tuple[list[dict[str, Any]], float]:
    """Return (functions, parse_confidence). Each function: name, start_line, loc."""
    clean = _strip_comments_strings(source)
    lines = clean.splitlines()
    functions: list[dict[str, Any]] = []
    parsed_ok = 0
    total_candidates = 0
    for m in _FUNC_RE.finditer(clean):
        name = m.group("name")
        if name in ("if", "for", "while", "switch", "return", "sizeof"):
            continue
        total_candidates += 1
        start_line = clean[: m.start()].count("\n") + 1
        # Find the opening brace and match it.
        brace_pos = clean.find("{", m.end() - 1)
        if brace_pos == -1 or brace_pos > m.end() + 200:
            continue  # probably a declaration, not a definition
        depth = 0
        end_pos = None
        for i in range(brace_pos, len(clean)):
            if clean[i] == "{":
                depth += 1
            elif clean[i] == "}":
                depth -= 1
                if depth == 0:
                    end_pos = i
                    break
        if end_pos is None:
            continue  # unbalanced — do not guess
        parsed_ok += 1
        end_line = clean[:end_pos].count("\n") + 1
        # LOC: non-blank lines in the body.
        body_lines = [ln for ln in lines[start_line - 1 : end_line] if ln.strip()]
        functions.append({"name": name, "start_line": start_line, "loc": len(body_lines)})
    confidence = (parsed_ok / total_candidates) if total_candidates else 0.0
    return functions, confidence


def analyze(ctx: AnalyzerContext) -> Any:
    params = {**DEFAULTS, **ctx.metric.params}
    try:
        source = Path(ctx.source_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="failed",
            diagnostics={"reason": f"could not read source: {exc}"},
        )

    functions, confidence = _extract_functions(source)
    exclude_main = bool(params["exclude_main_from_length"])
    score = 1.0
    findings: list[str] = []
    warnings: list[str] = []

    if confidence < 0.5 and functions:
        warnings.append(
            f"function parsing is uncertain (confidence {confidence:.2f}); "
            "results are approximate"
        )

    n = len(functions)
    if n == 0:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            diagnostics={"reason": "no function definitions detected"},
            warnings=["could not detect any functions — modularity cannot be assessed"],
        )

    if n < int(params["min_functions"]):
        score -= float(params["single_function_penalty"])
        findings.append(
            f"only {n} function(s) detected (minimum expected "
            f"{params['min_functions']}) — consider decomposing into helpers"
        )

    max_loc = int(params["max_loc_per_function"])
    long_fns = [
        f
        for f in functions
        if f["loc"] > max_loc and not (exclude_main and f["name"] == "main")
    ]
    for f in long_fns:
        score -= float(params["long_function_penalty"])
        findings.append(
            f"function '{f['name']}' is {f['loc']} LOC (limit {max_loc}) — "
            "consider splitting it"
        )

    score = max(float(params["score_floor"]), score)
    locs = [f["loc"] for f in functions]

    evidence_refs = [
        ctx.add_evidence(
            ANALYZER_ID,
            "modularity",
            {
                "function_count": n,
                "functions": functions,
                "parse_confidence": round(confidence, 3),
                "max_loc": max(locs),
                "avg_loc": round(sum(locs) / len(locs), 2),
                "exclude_main_from_length": exclude_main,
            },
            confidence=confidence,
        )
    ]

    return build_result(
        metric=ctx.metric,
        analyzer_id=ANALYZER_ID,
        status="completed",
        score=score,
        measurements={
            "function_count": n,
            "max_loc": max(locs),
            "avg_loc": round(sum(locs) / len(locs), 2),
            "parse_confidence": round(confidence, 3),
            "long_functions": [f["name"] for f in long_fns],
        },
        evidence_refs=evidence_refs,
        findings=findings,
        warnings=warnings,
        limitations=[
            "regex-based parsing is approximate; complex macros or K&R style "
            "may reduce parse confidence"
        ],
    )
