"""Compiler analyzer: clean / warnings / errors via real gcc runs."""

from conftest import make_analyzer_ctx

from autograder.app.analyzers.compiler import analyzer as compiler


def _ctx_for(sandbox_executor, tmp_path, source_name, **kw):
    from autograder.app.sandbox import compile_c
    from autograder.app.core.schemas import CompileConfig
    from conftest import FIXTURES_C

    src = FIXTURES_C / source_name
    out = tmp_path / "build" / source_name.replace(".c", "")
    # compile_c raises nothing on failure; we want the raw CompileResult.
    result = compile_c(
        str(src), str(out), CompileConfig(), sandbox_executor,
        allowed_dir=str(FIXTURES_C),
    )
    return make_analyzer_ctx(
        sandbox_executor, tmp_path, "compiler", source_name,
        compile_result=result,
        binary_path=result.binary_path,
        **kw,
    ), result


def test_clean_compile_scores_full(sandbox_executor, tmp_path):
    ctx, _ = _ctx_for(sandbox_executor, tmp_path, "correct_add.c")
    result = compiler.analyze(ctx)
    assert result.status == "completed"
    assert result.score == 1.0
    assert result.findings == []


def test_warnings_degrade_score(sandbox_executor, tmp_path):
    ctx, _ = _ctx_for(sandbox_executor, tmp_path, "warnings_add.c")
    result = compiler.analyze(ctx)
    assert result.status == "completed"
    assert result.score < 1.0
    assert result.measurements["warning_count"] >= 1
    assert any("warning" in f for f in result.findings)


def test_broken_source_is_failed_not_zero(sandbox_executor, tmp_path):
    broken = "int main( { this is not C at all"
    src = tmp_path / "broken.c"
    src.write_text(broken)
    from autograder.app.sandbox import compile_c
    from autograder.app.core.schemas import CompileConfig

    out = tmp_path / "build" / "broken"
    cres = compile_c(
        str(src), str(out), CompileConfig(), sandbox_executor,
        allowed_dir=str(tmp_path),
    )
    assert not cres.ok
    ctx = make_analyzer_ctx(
        sandbox_executor, tmp_path, "compiler", "correct_add.c",
        compile_result=cres, binary_path=None,
    )
    result = compiler.analyze(ctx)
    assert result.status == "failed"
    assert result.score is None
    assert any("error" in f for f in result.findings)


def test_no_compile_result_is_inconclusive(sandbox_executor, tmp_path):
    ctx = make_analyzer_ctx(
        sandbox_executor, tmp_path, "compiler", "correct_add.c",
        compile_result=None,
    )
    result = compiler.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None
