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


def test_checks_run_discloses_exactly_what_ran(sandbox_executor, tmp_path):
    import shutil

    from autograder.app.analyzers.static_analysis.checks import BUILTIN_CHECK_IDS

    ctx = _ctx(sandbox_executor, tmp_path, "correct_add.c")
    result = static.analyze(ctx)
    checks_run = result.measurements["checks_run"]
    # Every built-in check id is listed — they all execute on every source.
    for cid in BUILTIN_CHECK_IDS:
        assert cid in checks_run
    # Exactly one cppcheck entry, stating its true outcome.
    cpp_entries = [c for c in checks_run if c.startswith("cppcheck:")]
    assert len(cpp_entries) == 1
    if shutil.which("cppcheck") is None:
        assert cpp_entries == ["cppcheck: not installed"]
        assert result.measurements["cppcheck_state"] == "not_installed"
    else:
        assert cpp_entries == ["cppcheck: ran in sandbox"]
        assert result.measurements["cppcheck_state"] == "ran"


def test_cppcheck_disabled_by_param_is_disclosed(sandbox_executor, tmp_path):
    ctx = _ctx(
        sandbox_executor, tmp_path, "correct_add.c",
        metric_params={"use_cppcheck": False},
    )
    result = static.analyze(ctx)
    assert result.measurements["cppcheck_status"] == "not_attempted"
    assert result.measurements["cppcheck_state"] == "not_attempted"
    assert "cppcheck: not attempted (disabled)" in result.measurements["checks_run"]


def test_run_cppcheck_outcome_states(monkeypatch, tmp_path):
    # _run_cppcheck must distinguish why cppcheck did not run — a sandbox
    # failure or timeout is not the same fact as "not installed".
    from types import SimpleNamespace

    from autograder.app.analyzers.static_analysis import analyzer as sa

    monkeypatch.setattr(sa.shutil, "which", lambda name: "/usr/bin/cppcheck")
    src = str(tmp_path / "x.c")

    class RaisingExecutor:
        def run(self, *a, **kw):
            raise RuntimeError("launcher died")

    findings, state, _ = sa._run_cppcheck(src, 5.0, RaisingExecutor())
    assert findings is None and state == "failed"

    class TimeoutExecutor:
        def run(self, *a, **kw):
            return SimpleNamespace(timed_out=True, stderr="", exit_code=None)

    findings, state, _ = sa._run_cppcheck(src, 5.0, TimeoutExecutor())
    assert findings is None and state == "timed_out"

    class BadExitExecutor:
        def run(self, *a, **kw):
            return SimpleNamespace(timed_out=False, stderr="", exit_code=2)

    findings, state, _ = sa._run_cppcheck(src, 5.0, BadExitExecutor())
    assert findings is None and state == "failed"

    class OkExecutor:
        def run(self, *a, **kw):
            return SimpleNamespace(
                timed_out=False, exit_code=0,
                stderr="3:warning:unusedVariable:Unused variable x\n",
            )

    findings, state, _ = sa._run_cppcheck(src, 5.0, OkExecutor())
    assert state == "ran"
    assert len(findings) == 1 and findings[0].tool == "cppcheck"

    findings, state, _ = sa._run_cppcheck(src, 5.0, None)
    assert findings is None and state == "no_executor"
