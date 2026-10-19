"""Static-analysis analyzer: built-in checks + honest cppcheck handling."""

from conftest import make_analyzer_ctx

from autograder.app.analyzers.static_analysis import analyzer as static
from autograder.app.analyzers.static_analysis.checks import run_builtin_checks


def _ctx(sandbox_executor, tmp_path, source_name, **kw):
    return make_analyzer_ctx(
        sandbox_executor, tmp_path, "static_analysis", source_name, **kw
    )


def test_builtin_checks_find_gets():
    findings = run_builtin_checks('int main(){ char b[8]; gets(b); return 0; }')
    ids = [f.check_id for f in findings]
    assert "builtin.dangerous-gets" in ids
    assert findings[0].severity == "high"


def test_builtin_checks_find_strcpy_and_scanf():
    src = '#include <string.h>\nint main(){ char d[8]; char s[8]; strcpy(d,s); scanf("%s", s); return 0; }'
    findings = run_builtin_checks(src)
    ids = {f.check_id for f in findings}
    assert "builtin.unbounded-strcpy" in ids
    assert "builtin.scanf-no-width" in ids


def test_builtin_checks_clean_code():
    findings = run_builtin_checks(
        '#include <stdio.h>\nint main(void){ printf("hi\\n"); return 0; }\n'
    )
    assert findings == []


def test_unsafe_fixture_flagged(sandbox_executor, tmp_path):
    ctx = _ctx(sandbox_executor, tmp_path, "unsafe.c")
    result = static.analyze(ctx)
    assert result.status == "completed"
    assert result.score < 1.0
    text = "\n".join(result.findings)
    assert "gets" in text
    assert "strcpy" in text
    assert result.measurements["builtin_count"] >= 2


def test_clean_fixture_scores_full(sandbox_executor, tmp_path):
    ctx = _ctx(sandbox_executor, tmp_path, "correct_add.c")
    result = static.analyze(ctx)
    assert result.status == "completed"
    assert result.score == 1.0


def test_cppcheck_absence_is_honest(sandbox_executor, tmp_path):
    import shutil

    ctx = _ctx(sandbox_executor, tmp_path, "correct_add.c")
    result = static.analyze(ctx)
    if shutil.which("cppcheck") is None:
        assert result.measurements["cppcheck_status"] == "unavailable"
        assert any("cppcheck is not installed" in w for w in result.limitations)
        # Nothing fabricated: no cppcheck findings claimed.
        assert result.measurements["cppcheck_count"] == 0
    else:
        assert result.measurements["cppcheck_status"] == "ran"
