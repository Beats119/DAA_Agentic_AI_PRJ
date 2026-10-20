"""Correctness analyzer: real compilation + sandboxed runs of fixtures."""

import pytest

from conftest import compile_fixture, make_analyzer_ctx, trusted_case

from autograder.app.analyzers.correctness import analyzer as correctness


def _cases():
    # Under the default "exact" whitespace policy, expected outputs must
    # match the program's bytes precisely (including the trailing newline).
    return [
        trusted_case("t1", "2 3", "5\n"),
        trusted_case("t2", "0 0", "0\n"),
        trusted_case("t3", "-4 10", "6\n"),
    ]


def _ctx(sandbox_executor, tmp_path, source_name, cases, **kw):
    binary, _ = compile_fixture(sandbox_executor, tmp_path, source_name)
    return make_analyzer_ctx(
        sandbox_executor, tmp_path, "correctness", source_name,
        binary_path=binary, test_cases=cases, **kw,
    )


def test_correct_program_scores_full(sandbox_executor, tmp_path):
    ctx = _ctx(sandbox_executor, tmp_path, "correct_add.c", _cases())
    result = correctness.analyze(ctx)
    assert result.status == "completed"
    assert result.score == 1.0
    assert result.measurements["passed_trusted"] == 3
    assert result.measurements["total_trusted"] == 3
    assert len(result.evidence_refs) == 3
    assert result.findings == []


def test_buggy_program_scores_zero(sandbox_executor, tmp_path):
    ctx = _ctx(sandbox_executor, tmp_path, "buggy_add.c", _cases())
    result = correctness.analyze(ctx)
    assert result.status == "completed"
    assert result.score == 0.0
    assert len(result.findings) == 3
    # Failing tests name input, expected and actual.
    assert "2 3" in result.findings[0]
    assert "expected='5\\n'" in result.findings[0]
    assert "actual='6\\n'" in result.findings[0]


def test_no_trusted_tests_is_inconclusive_not_zero(sandbox_executor, tmp_path):
    binary, _ = compile_fixture(sandbox_executor, tmp_path, "correct_add.c")
    ctx = make_analyzer_ctx(
        sandbox_executor, tmp_path, "correctness", "correct_add.c",
        binary_path=binary, test_cases=[],
    )
    result = correctness.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None


def test_missing_binary_is_failed_not_zero(sandbox_executor, tmp_path):
    ctx = make_analyzer_ctx(
        sandbox_executor, tmp_path, "correctness", "correct_add.c",
        binary_path=None, test_cases=_cases(),
    )
    result = correctness.analyze(ctx)
    assert result.status == "failed"
    assert result.score is None


def test_provisional_tests_excluded_from_score(sandbox_executor, tmp_path):
    from autograder.app.core.schemas import TestCase

    provisional = TestCase.model_validate(
        {
            "id": "p1", "input": "1 1", "input_category": "normal",
            "expected": {
                "value": "999\n",  # deliberately wrong; must not affect score
                "provenance": {"tier": "tier3_llm", "method": "llm", "detail": ""},
            },
            "status": "provisional",
        }
    )
    ctx = _ctx(
        sandbox_executor, tmp_path, "correct_add.c", _cases()[:1],
        metric_params={"run_exploratory": True},
    )
    ctx.test_cases.append(provisional)
    result = correctness.analyze(ctx)
    assert result.status == "completed"
    assert result.score == 1.0  # only the 1 trusted test counts
    assert result.measurements["total_trusted"] == 1
    assert result.measurements["exploratory"]["ran"] == 1


def test_normalize_trailing_policy(sandbox_executor, tmp_path):
    cases = [trusted_case("t1", "2 3", "5")]  # no trailing newline in expected
    ctx = _ctx(
        sandbox_executor, tmp_path, "correct_add.c", cases,
        rules_overrides={"comparison_policy": {"whitespace": "normalize_trailing"}},
    )
    result = correctness.analyze(ctx)
    assert result.status == "completed"
    assert result.score == 1.0


# ---------------------------------------------------------------------------
# D10: shared execution budget
# ---------------------------------------------------------------------------

def test_exec_budget_limits_trusted_runs(sandbox_executor, tmp_path):
    """With an execution budget of 1, only the first trusted test runs;
    the rest are disclosed as not-executed (never silently failed), and
    the score covers only what actually ran."""
    from autograder.app.testing.generation.budgets import BudgetTracker

    ctx = _ctx(sandbox_executor, tmp_path, "correct_add.c", _cases())
    ctx.budget_tracker = BudgetTracker(token_budget=10**9, exec_budget=1)
    result = correctness.analyze(ctx)
    assert result.status == "completed"
    assert result.measurements["total_trusted"] == 1  # executed
    assert result.measurements["trusted_total"] == 3
    assert result.measurements["not_executed_trusted"] == 2
    assert result.score == 1.0  # the one executed test passed
    assert any("execution budget exhausted" in w for w in result.warnings)
    assert any("not executed" in lim for lim in result.limitations)


def test_exec_budget_zero_trusted_runs_is_inconclusive(sandbox_executor, tmp_path):
    """Budget exhausted before ANY trusted test runs -> inconclusive,
    not a manufactured zero."""
    from autograder.app.testing.generation.budgets import BudgetTracker

    ctx = _ctx(sandbox_executor, tmp_path, "correct_add.c", _cases())
    ctx.budget_tracker = BudgetTracker(token_budget=10**9, exec_budget=0)
    result = correctness.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None
    assert any("execution budget exhausted" in w for w in result.warnings)
