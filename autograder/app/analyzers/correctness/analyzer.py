"""Correctness analyzer: run trusted tests against the student binary.

Only tests whose oracle status is ``trusted`` contribute to the confirmed
correctness score. ``provisional``/``inconclusive`` tests may be executed for
exploration (controlled by ``metric.params["run_exploratory"]``, default
False) and are reported under separate stats — they NEVER affect the score.

Score = passed_trusted / total_trusted. With zero trusted tests the result is
``inconclusive`` (score None), never 0.0.
"""

from __future__ import annotations

from typing import Any

from ...core.comparison import compare_output
from ...core.schemas import TestCase
from ...sandbox.executor import ExecLimits
from ..base import AnalyzerContext, build_result

ANALYZER_ID = "correctness"

DEFAULTS = {
    "run_exploratory": False,
    "per_test_timeout_s": None,  # None -> derive from rules.limits.time_limit_s
    "per_test_memory_mb": None,  # None -> derive from rules.limits.memory_limit_mb
}


def _limits_for(rules, params: dict[str, Any]) -> ExecLimits:
    wall = params.get("per_test_timeout_s") or rules.limits.time_limit_s
    mem = params.get("per_test_memory_mb") or rules.limits.memory_limit_mb
    return ExecLimits(
        wall_time_s=float(wall),
        cpu_time_s=float(wall) * 4,
        memory_mb=int(mem),
        nproc=rules.limits.process_limit,
        output_bytes=rules.limits.output_limit_bytes,
    )


def _run_one(ctx: AnalyzerContext, binary: str, tc: TestCase, limits: ExecLimits) -> dict[str, Any]:
    """Execute one test case; return a structured per-test record."""
    try:
        result = ctx.executor.run([binary], tc.input, limits)
    except Exception as exc:  # sandbox refusal etc. — recorded, not hidden
        return {
            "id": tc.id,
            "input": tc.input,
            "expected": tc.expected.value,
            "actual": None,
            "passed": False,
            "oracle_status": tc.status,
            "oracle_provenance": tc.expected.provenance.model_dump(),
            "error": f"execution failed: {exc}",
            "timed_out": False,
            "signal": None,
        }
    actual = result.stdout
    if result.timed_out or result.signal is not None or result.exit_code != 0:
        passed = False
    else:
        passed = compare_output(actual, tc.expected.value, ctx.rules.comparison_policy)
    return {
        "id": tc.id,
        "input": tc.input,
        "expected": tc.expected.value,
        "actual": actual,
        "passed": passed,
        "oracle_status": tc.status,
        "oracle_provenance": tc.expected.provenance.model_dump(),
        "timed_out": result.timed_out,
        "signal": result.signal,
        "exit_code": result.exit_code,
        "wall_s": round(result.wall_s, 4),
    }


def analyze(ctx: AnalyzerContext) -> Any:
    params = {**DEFAULTS, **ctx.metric.params}
    if ctx.binary_path is None:
        # Student code failed to compile — a failure of the submission, but
        # the analyzer cannot produce correctness evidence from a missing binary.
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="failed",
            diagnostics={"reason": "no compiled binary available (compilation failed)"},
            warnings=["correctness could not be measured: submission did not compile"],
        )

    trusted = [tc for tc in ctx.test_cases if tc.status == "trusted"]
    exploratory = (
        [tc for tc in ctx.test_cases if tc.status in ("provisional", "inconclusive")]
        if params["run_exploratory"]
        else []
    )

    if not trusted:
        # No trustworthy oracle evidence -> honest inconclusive, never 0.0.
        ev_id = ctx.add_evidence(
            ANALYZER_ID,
            "no_trusted_tests",
            {
                "total_candidates": len(ctx.test_cases),
                "status_breakdown": {
                    s: sum(1 for tc in ctx.test_cases if tc.status == s)
                    for s in ("trusted", "provisional", "rejected", "inconclusive")
                },
            },
            affects_grade=False,
        )
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            evidence_refs=[ev_id],
            diagnostics={"reason": "no tests with a trusted oracle; cannot confirm correctness"},
            warnings=[
                "no trusted oracle evidence — correctness is inconclusive, "
                "not zero (see oracle provenance in the report)"
            ],
            limitations=[
                "only tests with oracle status 'trusted' contribute confirmed "
                "correctness evidence"
            ],
        )

    limits = _limits_for(ctx.rules, params)
    per_test: list[dict[str, Any]] = []
    evidence_refs: list[str] = []
    budget_stopped_at: int | None = None
    for idx, tc in enumerate(trusted):
        # D10: every sandbox execution is charged to the shared per-run
        # budget BEFORE it happens. Exhaustion stops execution; tests that
        # never ran are disclosed, never silently scored as failures.
        if ctx.budget_tracker is not None and not ctx.budget_tracker.try_consume_exec():
            budget_stopped_at = idx
            break
        rec = _run_one(ctx, ctx.binary_path, tc, limits)
        per_test.append(rec)
        evidence_refs.append(
            ctx.add_evidence(
                ANALYZER_ID,
                "test_result",
                rec,
                confidence=1.0,
            )
        )

    if budget_stopped_at is not None and not per_test:
        # The budget ran out before ANY trusted test could execute: no
        # correctness evidence exists — inconclusive, not zero.
        ev_id = ctx.add_evidence(
            ANALYZER_ID,
            "exec_budget_exhausted",
            {"trusted_total": len(trusted), "executed": 0},
            affects_grade=False,
        )
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            evidence_refs=[ev_id],
            diagnostics={
                "reason": "execution budget exhausted before any trusted "
                          "test could run"
            },
            warnings=[
                "execution budget exhausted — no trusted test executed; "
                "correctness is inconclusive, not zero"
            ],
            limitations=[
                "only tests with oracle status 'trusted' contribute confirmed "
                "correctness evidence"
            ],
        )

    exploratory_stats: dict[str, Any] = {}
    if exploratory:
        exp_results = []
        for tc in exploratory:
            if (ctx.budget_tracker is not None
                    and not ctx.budget_tracker.try_consume_exec()):
                exploratory_stats["budget_exhausted"] = True
                break
            exp_results.append(_run_one(ctx, ctx.binary_path, tc, limits))
        exploratory_stats.update({
            "ran": len(exp_results),
            "passed": sum(1 for r in exp_results if r["passed"]),
            "note": "exploratory only — excluded from the confirmed score",
            "results": exp_results,
        })

    passed = sum(1 for r in per_test if r["passed"])
    total = len(per_test)
    score = passed / total if total else 0.0
    failing = [r for r in per_test if not r["passed"]]

    findings: list[str] = []
    for r in failing:
        findings.append(
            f"test {r['id']} failed: input={r['input']!r} "
            f"expected={r['expected']!r} actual={r['actual']!r} "
            f"(oracle: {r['oracle_provenance']['tier']}/{r['oracle_provenance']['method']})"
        )

    extra_warnings: list[str] = []
    extra_limitations: list[str] = []
    if budget_stopped_at is not None:
        not_run = len(trusted) - len(per_test)
        extra_warnings.append(
            f"execution budget exhausted: {not_run} of {len(trusted)} "
            "trusted test(s) were not executed; the score covers only "
            "the executed tests"
        )
        extra_limitations.append(
            f"score reflects {len(per_test)} of {len(trusted)} trusted "
            "tests; the remainder were not executed (execution budget)"
        )

    return build_result(
        metric=ctx.metric,
        analyzer_id=ANALYZER_ID,
        status="completed",
        score=score,
        measurements={
            "passed_trusted": passed,
            "total_trusted": total,
            "trusted_total": len(trusted),
            "not_executed_trusted": len(trusted) - total,
            "pass_rate": score,
            "exploratory": exploratory_stats,
        },
        evidence_refs=evidence_refs,
        findings=findings,
        warnings=extra_warnings,
        limitations=[
            "score reflects only tests with a trusted oracle; "
            f"{len(exploratory)} exploratory test(s) excluded from scoring"
        ] + extra_limitations,
        execution_metadata={
            "per_test_limits": limits.model_dump(),
            "comparison_policy": ctx.rules.comparison_policy.model_dump(),
        },
    )
