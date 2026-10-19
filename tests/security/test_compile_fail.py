"""Compiler: broken C -> structured diagnostics; hostile-but-valid C compiles."""

import pytest

from autograder.app.core.schemas import CompileConfig
from autograder.app.sandbox import SandboxError, compile_c

BROKEN_SRC = "int main( { return 0; }\n"
VALID_HOSTILE_SRC = r"""
#include <unistd.h>
int main() { while (1) fork(); return 0; }
"""


def test_broken_c_gives_structured_diagnostics(executor, tmp_path):
    src = tmp_path / "broken.c"
    src.write_text(BROKEN_SRC)
    result = compile_c(src, tmp_path / "out", CompileConfig(), executor, allowed_dir=tmp_path)

    assert result.ok is False
    assert result.binary_path is None
    assert result.diagnostics, "expected parsed diagnostics"
    assert any(d.kind == "error" for d in result.diagnostics)
    assert result.raw_stderr, "raw stderr must always be kept"


def test_hostile_but_valid_c_compiles_under_limits(executor, tmp_path):
    src = tmp_path / "hostile.c"
    src.write_text(VALID_HOSTILE_SRC)
    result = compile_c(src, tmp_path / "out", CompileConfig(), executor, allowed_dir=tmp_path)

    assert result.ok is True, f"valid C must compile: {result.raw_stderr[:500]}"
    assert result.binary_sha256 and len(result.binary_sha256) == 64


def test_source_outside_allowed_dir_rejected(executor, tmp_path):
    outside = tmp_path.parent / "outside.c"
    outside.write_text("int main(){return 0;}\n")
    try:
        with pytest.raises(SandboxError):
            compile_c(outside, tmp_path / "out", CompileConfig(), executor,
                      allowed_dir=tmp_path)
    finally:
        outside.unlink(missing_ok=True)


def test_missing_source_rejected(executor, tmp_path):
    with pytest.raises(SandboxError):
        compile_c(tmp_path / "nope.c", tmp_path / "out", CompileConfig(), executor,
                  allowed_dir=tmp_path)
