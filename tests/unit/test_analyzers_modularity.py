"""Modularity analyzer: function decomposition heuristics."""

from conftest import make_analyzer_ctx

from autograder.app.analyzers.modularity import analyzer as modularity
from autograder.app.analyzers.modularity.analyzer import _extract_functions

LONG_FN_SRC = """#include <stdio.h>

int helper(int x) {
    return x * 2;
}

int very_long_function(int n) {
""" + "\n".join(f"    int v{i} = {i} * n;" for i in range(50)) + """
    return v0 + v49;
}

int main(void) {
    printf("%d\\n", very_long_function(3));
    return 0;
}
"""


def _ctx(sandbox_executor, tmp_path, source_name="correct_add.c", **kw):
    return make_analyzer_ctx(
        sandbox_executor, tmp_path, "modularity", source_name, **kw
    )


def test_extract_functions_finds_both():
    src = open("/home/hatch/workspace/daa_prj/tests/fixtures/c/correct_add.c").read()
    fns, conf = _extract_functions(src)
    names = {f["name"] for f in fns}
    assert names == {"add", "main"}
    assert conf == 1.0


def test_well_decomposed_scores_full(sandbox_executor, tmp_path):
    ctx = _ctx(sandbox_executor, tmp_path, source_name="correct_add.c")
    result = modularity.analyze(ctx)
    assert result.status == "completed"
    assert result.score == 1.0
    assert result.measurements["function_count"] == 2


def test_single_function_penalized(sandbox_executor, tmp_path):
    ctx = _ctx(sandbox_executor, tmp_path, source_name="loop_heavy.c")
    result = modularity.analyze(ctx)
    assert result.status == "completed"
    assert result.score < 1.0
    assert any("only 1 function" in f for f in result.findings)


def test_long_function_flagged_by_name(sandbox_executor, tmp_path):
    ctx = _ctx(
        sandbox_executor, tmp_path, source_text=LONG_FN_SRC,
    )
    result = modularity.analyze(ctx)
    assert result.status == "completed"
    assert result.score < 1.0
    assert any("very_long_function" in f for f in result.findings)
    # main() is excluded from length checks by documented default.
    assert not any("'main'" in f and "LOC" in f for f in result.findings)


def test_no_functions_is_inconclusive(sandbox_executor, tmp_path):
    ctx = _ctx(sandbox_executor, tmp_path, source_text="/* just a comment */\nint x = 3;\n")
    result = modularity.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None
