"""Correctness analyzer: real compilation + sandboxed runs of fixtures."""


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


# ---------------------------------------------------------------------------
# D15: sandbox infrastructure failures are NOT student test failures
# ---------------------------------------------------------------------------

class _RaisingExecutor:
    """TEST-ONLY stub executor: every run refuses (sandbox down)."""

    def run(self, argv, stdin_text, limits, **kw):
        from autograder.app.sandbox.executor import SandboxError

        raise SandboxError("sandbox unavailable: landlock not supported")


class _FlakyExecutor:
    """TEST-ONLY stub executor: delegates to the real executor except for
    one input, where the launcher 'fails' (infrastructure, not student)."""

    def __init__(self, real, bad_input):
        self._real = real
        self._bad = bad_input

    def run(self, argv, stdin_text, limits, **kw):
        if stdin_text == self._bad:
            from autograder.app.sandbox.executor import SandboxError

            raise SandboxError("sandbox launcher exited 125")
        return self._real.run(argv, stdin_text, limits, **kw)


class _UnenforcedExecutor:
    """TEST-ONLY stub executor: returns a result whose enforcement report
    shows a mandatory mechanism missing."""

    def run(self, argv, stdin_text, limits, **kw):
        from types import SimpleNamespace

        return SimpleNamespace(
            stdout="5\n", stderr="", exit_code=0, signal=None,
            timed_out=False, wall_s=0.01,
            enforcement=SimpleNamespace(
                landlock_fs=False, seccomp_filter=True, network_isolated=True
            ),
        )


def _ctx_with_executor(sandbox_executor, tmp_path, executor):
    binary, _ = compile_fixture(sandbox_executor, tmp_path, "correct_add.c")
    ctx = make_analyzer_ctx(
        sandbox_executor, tmp_path, "correctness", "correct_add.c",
        binary_path=binary, test_cases=_cases(),
    )
    ctx.executor = executor
    return ctx


def test_all_infra_errors_is_inconclusive_not_zero(sandbox_executor, tmp_path):
    ctx = _ctx_with_executor(sandbox_executor, tmp_path, _RaisingExecutor())
    result = correctness.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None
    assert result.findings == []
    assert "infrastructure failed for all" in result.diagnostics["reason"]
    # Infra records are minted under their own evidence kind, never as
    # test_result (feedback rebuilds trusted results from test_result).
    kinds = [ev.kind for ev in ctx.evidence]
    assert kinds and all(k == "test_infra_error" for k in kinds)


def test_some_infra_errors_excluded_from_score(sandbox_executor, tmp_path):
    ctx = _ctx_with_executor(
        sandbox_executor, tmp_path, _FlakyExecutor(sandbox_executor, "0 0")
    )
    result = correctness.analyze(ctx)
    assert result.status == "completed"
    # Both tests that actually ran passed; the infra-failed one is
    # excluded — not counted as a failure.
    assert result.score == 1.0
    assert result.measurements["passed_trusted"] == 2
    assert result.measurements["total_trusted"] == 2
    assert result.measurements["infra_errors"] == 1
    assert result.findings == []
    assert any("infrastructure failed" in w for w in result.warnings)
    assert any("infrastructure failure" in lim for lim in result.limitations)


def test_unenforced_run_is_infra_error(sandbox_executor, tmp_path):
    ctx = _ctx_with_executor(sandbox_executor, tmp_path, _UnenforcedExecutor())
    result = correctness.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None


def test_feedback_trusted_results_skip_infra_records():
    # D15 defense in depth: even a legacy test_result record carrying an
    # infra_error outcome must not be rebuilt as a student failure.
    from types import SimpleNamespace

    from autograder.app.orchestration.feedback import build_trusted_results

    class _Registry:
        def all(self):
            return [
                SimpleNamespace(
                    analyzer="correctness", kind="test_result", id="ev:x:1",
                    data={"id": "t1", "passed": False, "outcome": "infra_error",
                          "input": "1", "expected": "2", "actual": None,
                          "oracle_provenance": {}},
                ),
                SimpleNamespace(
                    analyzer="correctness", kind="test_result", id="ev:x:2",
                    data={"id": "t2", "passed": True, "outcome": "run_ok",
                          "input": "1", "expected": "2", "actual": "2",
                          "oracle_provenance": {}},
                ),
            ]

    trusted, failing = build_trusted_results(_Registry())
    assert set(trusted) == {"t2"}
    assert failing == []
