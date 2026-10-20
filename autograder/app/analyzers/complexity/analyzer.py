"""Complexity analyzer: static inference + honest timing.

Two evidence legs:

(a) STATIC (primary): loop-nesting depth and recursion detection over the
    source -> a conservative inferred Big-O class. This leg drives scoring
    when ``rules.complexity.required`` is set. When no required class is
    known (rules and specification both silent), there is nothing to grade
    the inference against: the result is ``inconclusive`` with the inferred
    class recorded as information — never an invented score (D11).

(b) TIMING (corroboration only): the binary is run on the trusted test
    inputs and wall times are recorded. Per the project spec (sec 13.6),
    timing measurements are NEVER presented as proof of a Big-O class and
    NEVER change the score. A suspiciously slow test only adds a warning.

If the static leg cannot determine a class (e.g. recursion, function
pointers, unparseable control flow), the result is ``inconclusive`` —
an explicit honest outcome, not a guessed score.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...sandbox.executor import ExecLimits
from ..base import AnalyzerContext, build_result

ANALYZER_ID = "complexity"

DEFAULTS = {
    "mismatch_penalty": 0.5,
    "slow_test_warn_s": 1.0,
    "score_floor": 0.0,
}

_LOOP_KW = re.compile(r"\b(for|while)\s*\(|\bdo\b")


def _strip(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    source = re.sub(r'"(?:[^"\\]|\\.)*"', '""', source)
    source = re.sub(r"'(?:[^'\\]|\\.)*'", "''", source)
    return source


def _max_loop_nesting(clean: str) -> int:
    """Approximate max nesting depth of for/while/do loops.

    Tracks brace depth plus a stack of active loop frames. A braceless loop
    body (single statement) is closed at the next ``;`` or at the end of a
    braced block. Semicolons inside ``for(;;)`` headers are ignored via
    parenthesis-depth tracking. Deterministic and conservative: when in
    doubt it counts the loop rather than missing it.
    """
    depth = 0
    paren_depth = 0
    max_nesting = 0
    stack: list[dict] = []  # frames: {"braceless": bool, "body_depth": int|None}
    pending_loop = False  # loop keyword seen, body not yet classified

    def _push_braceless() -> None:
        nonlocal max_nesting
        stack.append({"braceless": True, "body_depth": None})
        max_nesting = max(max_nesting, len(stack))

    for tok in re.finditer(r"[{}();]|\b(for|while)\s*\(|\bdo\b", clean):
        t = tok.group(0)
        if t == "{":
            depth += 1
            if pending_loop:
                stack.append({"braceless": False, "body_depth": depth})
                max_nesting = max(max_nesting, len(stack))
                pending_loop = False
        elif t == "}":
            # A braceless loop body ending here pops; braced loop bodies
            # whose block is closing pop too. Anything else stays.
            while stack:
                top = stack[-1]
                if top["braceless"] or top["body_depth"] == depth:
                    stack.pop()
                else:
                    break
            depth = max(0, depth - 1)
            pending_loop = False
        elif t == "(":
            paren_depth += 1
        elif t == ")":
            paren_depth = max(0, paren_depth - 1)
        elif t == ";":
            if paren_depth > 0:
                continue  # inside a for(;;) header — not a statement end
            if pending_loop:
                _push_braceless()  # braceless loop with single-statement body
                pending_loop = False
            while stack and stack[-1]["braceless"]:
                stack.pop()
        elif t == "do":
            if pending_loop:
                _push_braceless()
            pending_loop = True
        else:  # "for(" or "while(" — the "(" is consumed by this token
            paren_depth += 1
            if pending_loop:
                # Previous loop is braceless; its body is this loop statement.
                _push_braceless()
            pending_loop = True
    if pending_loop:
        # Trailing loop keyword with no visible body (e.g. macro) — count it.
        max_nesting = max(max_nesting, len(stack) + 1)
    return max_nesting


def _has_recursion(clean: str) -> bool:
    """Detect direct recursion: a function calling itself by name."""
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{", clean):
        name = m.group(1)
        # find matching close brace (approx) and search body for name(
        start = m.end() - 1
        depth = 0
        end = None
        for i in range(start, len(clean)):
            if clean[i] == "{":
                depth += 1
            elif clean[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        body = clean[m.end() : end]
        if re.search(rf"\b{re.escape(name)}\s*\(", body):
            return True
    return False


def _infer_class(nesting: int, recursive: bool) -> str | None:
    if recursive:
        return None  # cannot honestly infer; caller marks inconclusive
    return {0: "O(1)", 1: "O(n)", 2: "O(n^2)"}.get(nesting, "O(n^3+)")


def _normalize_class(s: str) -> str:
    return re.sub(r"\s+", "", s.strip().lower())


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
    clean = _strip(source)
    nesting = _max_loop_nesting(clean)
    recursive = _has_recursion(clean)
    inferred = _infer_class(nesting, recursive)

    # --- timing leg (corroboration only, never scores) ---
    timing: list[dict[str, Any]] = []
    timing_warnings: list[str] = []
    if ctx.binary_path and ctx.test_cases:
        limits = ExecLimits(
            wall_time_s=float(ctx.rules.limits.time_limit_s),
            memory_mb=int(ctx.rules.limits.memory_limit_mb),
            nproc=ctx.rules.limits.process_limit,
            output_bytes=ctx.rules.limits.output_limit_bytes,
        )
        for tc in ctx.test_cases:
            if tc.status != "trusted":
                continue
            try:
                res = ctx.executor.run([ctx.binary_path], tc.input, limits)
                timing.append({"test_id": tc.id, "wall_s": round(res.wall_s, 4)})
                if res.wall_s > float(params["slow_test_warn_s"]) and not res.timed_out:
                    timing_warnings.append(
                        f"test {tc.id} took {res.wall_s:.2f}s — possible "
                        "inefficiency (timing is not a complexity proof)"
                    )
            except Exception as exc:
                timing.append({"test_id": tc.id, "error": str(exc)[:120]})
    timing_note = (
        "timing measurements are recorded for corroboration only and are "
        "never used as proof of a Big-O class (spec sec 13.6)"
    )

    if inferred is None:
        ev_id = ctx.add_evidence(
            ANALYZER_ID,
            "complexity_unknown",
            {
                "max_loop_nesting": nesting,
                "recursion_detected": recursive,
                "timing": timing,
            },
            affects_grade=False,
        )
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            evidence_refs=[ev_id],
            diagnostics={
                "reason": "recursion detected: Big-O class cannot be honestly inferred statically"
            },
            warnings=timing_warnings + [timing_note],
            limitations=[timing_note],
        )

    required = ctx.rules.complexity.required

    ev_id = ctx.add_evidence(
        ANALYZER_ID,
        "complexity",
        {
            "inferred_class": inferred,
            "required_class": required,
            "max_loop_nesting": nesting,
            "recursion_detected": recursive,
            "timing": timing,
            "timing_caveat": timing_note,
        },
        confidence=0.8,
        affects_grade=bool(required),
    )
    measurements = {
        "inferred_class": inferred,
        "required_class": required,
        "max_loop_nesting": nesting,
        "recursion_detected": recursive,
        "timing_samples": len(timing),
    }

    if not required:
        # D11: no required class anywhere (rules + specification silent) —
        # the inferred class is information, not a grade. Awarding a score
        # here would manufacture marks from an unchecked heuristic.
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            measurements=measurements,
            evidence_refs=[ev_id],
            diagnostics={
                "reason": "no required complexity class specified; "
                          "inferred class recorded but not graded"
            },
            findings=[
                f"inferred {inferred}; no complexity requirement set in "
                "rules — not graded"
            ],
            warnings=timing_warnings + [timing_note],
            limitations=[
                "no required complexity class specified",
                timing_note,
            ],
        )

    findings: list[str] = []
    if _normalize_class(required) == _normalize_class(inferred):
        score = 1.0
        findings.append(f"inferred {inferred} matches required {required}")
    else:
        score = max(float(params["score_floor"]), 1.0 - float(params["mismatch_penalty"]))
        findings.append(
            f"inferred {inferred} does not match required {required}"
        )

    return build_result(
        metric=ctx.metric,
        analyzer_id=ANALYZER_ID,
        status="completed",
        score=score,
        measurements=measurements,
        evidence_refs=[ev_id],
        findings=findings,
        warnings=timing_warnings,
        limitations=[timing_note],
    )
