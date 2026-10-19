"""Compiler analyzer: turn compilation diagnostics into a quality score.

- Clean compile, no warnings -> 1.0
- Warnings degrade the score by ``warning_penalty`` each (floor ``score_floor``)
- Compile errors -> status ``failed`` (score None): there is no binary, so no
  compiler-quality score can be honestly given; diagnostics are preserved.
"""

from __future__ import annotations

from typing import Any

from ..base import AnalyzerContext, build_result

ANALYZER_ID = "compiler"

DEFAULTS = {
    "warning_penalty": 0.1,
    "score_floor": 0.0,
    "max_warnings_counted": 10,
}


def analyze(ctx: AnalyzerContext) -> Any:
    params = {**DEFAULTS, **ctx.metric.params}
    cr = ctx.compile_result
    if cr is None:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            diagnostics={"reason": "no compile result available in context"},
            warnings=["compiler analyzer ran without compilation evidence"],
        )

    evidence_refs = [
        ctx.add_evidence(
            ANALYZER_ID,
            "compile_result",
            {
                "ok": cr.ok,
                "returncode": cr.returncode,
                "compiler": cr.compiler,
                "binary_sha256": cr.binary_sha256,
                "wall_s": cr.wall_s,
                "timed_out": cr.timed_out,
                "diagnostics": [d.model_dump() for d in cr.diagnostics],
            },
            severity="error" if not cr.ok else None,
            confidence=1.0,
        )
    ]

    errors = [d for d in cr.diagnostics if d.kind == "error"]
    if not cr.ok or errors:
        findings = [
            f"{d.file or '?'}:{d.line or '?'}: error: {d.message}" for d in errors
        ] or ["compilation failed (see raw diagnostics)"]
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="failed",
            evidence_refs=evidence_refs,
            findings=findings,
            diagnostics={
                "reason": "compilation failed; no compiler-quality score is honest",
                "raw_stderr_tail": cr.raw_stderr[-2000:],
            },
        )

    warnings = [d for d in cr.diagnostics if d.kind == "warning"]
    counted = warnings[: int(params["max_warnings_counted"])]
    penalty = float(params["warning_penalty"])
    floor = float(params["score_floor"])
    score = max(floor, 1.0 - penalty * len(counted))

    findings = [
        f"{d.file or '?'}:{d.line or '?'}: warning: {d.message}" for d in counted
    ]
    if len(warnings) > len(counted):
        findings.append(f"... and {len(warnings) - len(counted)} further warning(s)")

    return build_result(
        metric=ctx.metric,
        analyzer_id=ANALYZER_ID,
        status="completed",
        score=score,
        measurements={
            "warning_count": len(warnings),
            "warnings_counted": len(counted),
            "warning_penalty": penalty,
        },
        evidence_refs=evidence_refs,
        findings=findings,
        execution_metadata={"compiler": cr.compiler, "binary_sha256": cr.binary_sha256},
    )
