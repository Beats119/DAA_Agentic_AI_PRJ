"""Shared fixtures and builders for unit tests."""

from __future__ import annotations

import pytest

from autograder.app.core.schemas import AnalyzerResult, MetricConfig, RuleSet


def base_rules_dict(**overrides):
    """Minimal valid rules mapping; override any part per-test."""
    data = {
        "problem_id": "p1",
        "assignment_ref": "assignments/p1.pdf",
        "version": "1.0.0",
        "limits": {"time_limit_s": 2.0, "memory_limit_mb": 256.0},
        "metrics": [
            {"name": "correctness", "analyzer": "correctness", "weight": 0.6},
            {"name": "compiler", "analyzer": "compiler", "weight": 0.4},
        ],
    }
    data.update(overrides)
    return data


def make_rules(metrics, **overrides):
    """Build a RuleSet from (name, analyzer, weight, required) tuples."""
    defs = []
    for m in metrics:
        name, analyzer, weight = m[0], m[1], m[2]
        required = m[3] if len(m) > 3 else True
        defs.append(
            {"name": name, "analyzer": analyzer, "weight": weight, "required": required}
        )
    data = base_rules_dict(metrics=defs)
    data.update(overrides)
    return RuleSet.model_validate(data)


def make_result(name, analyzer, score, status="completed"):
    return AnalyzerResult(
        metric_name=name,
        analyzer_id=analyzer,
        analyzer_version="1.0",
        status=status,
        score=score,
    )


# ---------------------------------------------------------------------------
# Analyzer test helpers (Phase 5)
# ---------------------------------------------------------------------------

from pathlib import Path as _Path  # noqa: E402


FIXTURES_C = _Path(__file__).resolve().parent.parent / "fixtures" / "c"
FIXTURES_CORPUS = _Path(__file__).resolve().parent.parent / "fixtures" / "corpus"


@pytest.fixture(scope="session")
def sandbox_executor(tmp_path_factory):
    from autograder.app.sandbox import SandboxExecutor

    ex = SandboxExecutor(base_dir=str(tmp_path_factory.mktemp("sandbox")))
    ex.ensure_available()  # fail fast: this host cannot sandbox at all
    return ex


def compile_fixture(executor, tmp_path, name, config=None):
    """Compile tests/fixtures/c/<name>; return (binary_path, CompileResult)."""
    from autograder.app.core.schemas import CompileConfig
    from autograder.app.sandbox import compile_c

    src = FIXTURES_C / name
    out = tmp_path / "build" / name.replace(".c", "")
    result = compile_c(
        str(src),
        str(out),
        config or CompileConfig(),
        executor,
        allowed_dir=str(FIXTURES_C),
    )
    assert result.ok, f"fixture {name} failed to compile: {result.raw_stderr[:500]}"
    return result.binary_path, result


def trusted_case(case_id, input_text, expected_text, tier="tier2_independent"):
    from autograder.app.core.schemas import TestCase

    return TestCase.model_validate(
        {
            "id": case_id,
            "input": input_text,
            "input_category": "normal",
            "rationale": "unit-test fixture",
            "requirement_refs": [],
            "expected": {
                "value": expected_text,
                "provenance": {
                    "tier": tier,
                    "method": "test_fixture",
                    "detail": "synthetic fixture, not a real oracle",
                },
            },
            "status": "trusted",
        }
    )


def make_analyzer_ctx(
    executor,
    tmp_path,
    analyzer,
    source_name,
    *,
    metric_params=None,
    test_cases=(),
    compile_result=None,
    binary_path=None,
    gateway=None,
    rules_overrides=None,
    source_text=None,
):
    """Build an AnalyzerContext for ``analyzer`` over a fixture program.

    ``source_name`` is a file in tests/fixtures/c, or ``source_text`` writes
    inline C to tmp_path (production code never sees these paths).
    """
    from autograder.app.analyzers.base import AnalyzerContext
    from autograder.app.core.schemas import MetricConfig
    from autograder.app.sandbox import compile_c

    if source_text is not None:
        src_path = tmp_path / f"{analyzer}_src.c"
        src_path.write_text(source_text)
    else:
        src_path = FIXTURES_C / source_name

    metric = MetricConfig(
        name=analyzer, analyzer=analyzer, weight=1.0, params=metric_params or {}
    )
    rules = make_rules([(analyzer, analyzer, 1.0)], **(rules_overrides or {}))
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    return AnalyzerContext(
        source_path=str(src_path),
        binary_path=binary_path,
        sanitizer_binary_path=None,
        rules=rules,
        metric=metric,
        run_dir=str(run_dir),
        executor=executor,
        compiler=compile_c,
        gateway=gateway,
        run_id="test",
        test_cases=list(test_cases),
        compile_result=compile_result,
    )
