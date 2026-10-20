"""Complexity analyzer: static inference, requirement matching, timing caveat."""

from conftest import make_analyzer_ctx, trusted_case

from autograder.app.analyzers.complexity import analyzer as complexity
from autograder.app.analyzers.complexity.analyzer import (
    _has_recursion,
    _infer_class,
    _max_loop_nesting,
)


def _ctx(sandbox_executor, tmp_path, source_name, cases=(), **kw):
    binary = None
    if cases:
        from conftest import compile_fixture

        binary, _ = compile_fixture(sandbox_executor, tmp_path, source_name)
    return make_analyzer_ctx(
        sandbox_executor, tmp_path, "complexity", source_name,
        binary_path=binary, test_cases=list(cases), **kw,
    )


def test_nesting_depth_detection():
    assert _max_loop_nesting("int main(){ return 0; }") == 0
    assert _max_loop_nesting("int main(){ for(;;){ } return 0; }") == 1
    nested = "int main(){ for(;;){ for(;;){ for(;;){ } } } return 0; }"
    assert _max_loop_nesting(nested) == 3
    braceless = "int main(){ for(;;) for(;;) x(); return 0; }"
    assert _max_loop_nesting(braceless) == 2
    mixed = "int main(){ while(x){ if(y){ for(;;){ } } } return 0; }"
    assert _max_loop_nesting(mixed) == 2


def test_recursion_detection():
    assert _has_recursion("int f(int n){ return n <= 1 ? 1 : n * f(n-1); }")
    assert not _has_recursion("int f(int n){ return n * 2; }")


def test_infer_class():
    assert _infer_class(0, False) == "O(1)"
    assert _infer_class(1, False) == "O(n)"
    assert _infer_class(2, False) == "O(n^2)"
    assert _infer_class(5, False) == "O(n^3+)"
    assert _infer_class(1, True) is None


def test_loop_heavy_inferred_quadratic(sandbox_executor, tmp_path):
    ctx = _ctx(
        sandbox_executor, tmp_path, "loop_heavy.c",
        rules_overrides={"complexity": {"required": "O(n^2)"}},
    )
    result = complexity.analyze(ctx)
    assert result.status == "completed"
    assert result.measurements["inferred_class"] == "O(n^2)"
    assert result.score == 1.0


def test_requirement_mismatch_penalized(sandbox_executor, tmp_path):
    ctx = _ctx(
        sandbox_executor, tmp_path, "loop_heavy.c",
        rules_overrides={"complexity": {"required": "O(n)"}},
    )
    result = complexity.analyze(ctx)
    assert result.status == "completed"
    assert result.score == 0.5  # 1.0 - default mismatch_penalty
    assert any("does not match" in f for f in result.findings)


def test_no_requirement_is_inconclusive(sandbox_executor, tmp_path):
    # D11 (Phase G): this test previously pinned `score_when_no_requirement
    # = 1.0` — a manufactured full score when the rules set no required
    # class. That behavior was wrong: with nothing to grade the inference
    # against, the analyzer must be inconclusive (score None) and say why.
    ctx = _ctx(sandbox_executor, tmp_path, "correct_add.c")
    result = complexity.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None
    # The inferred class is still recorded as information.
    assert result.measurements["inferred_class"] == "O(1)"
    assert result.measurements["required_class"] is None
    assert any(
        "no required complexity class specified" in lim
        for lim in result.limitations
    )


def test_recursion_is_inconclusive(sandbox_executor, tmp_path):
    src = "int f(int n){ return n<=1?1:n*f(n-1); }\nint main(void){ return f(5); }\n"
    ctx = _ctx(sandbox_executor, tmp_path, "correct_add.c", source_text=src)
    result = complexity.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None


def test_timing_leg_never_scores(sandbox_executor, tmp_path):
    cases = [trusted_case("t1", "30", "189225")]
    # A requirement is set so the run reaches `completed` (D11: without one
    # the analyzer is inconclusive); timing must still not affect the score.
    ctx = _ctx(
        sandbox_executor, tmp_path, "loop_heavy.c", cases,
        rules_overrides={"complexity": {"required": "O(n^2)"}},
    )
    result = complexity.analyze(ctx)
    assert result.status == "completed"
    assert result.measurements["timing_samples"] == 1
    # The timing caveat must be stated in limitations.
    assert any("never used as proof" in lim for lim in result.limitations)
