"""Filesystem escape: writes outside the run dir must fail (Landlock)."""

import os
from pathlib import Path

ESCAPE_SRC = r"""
#include <stdio.h>
int main() {
    FILE *f = fopen("/tmp/pwned_by_sandbox_test", "w");
    if (f) { fputs("pwned", f); fclose(f); }
    FILE *g = fopen("../../escape", "w");
    if (g) { fputs("pwned", g); fclose(g); }
    FILE *h = fopen("legit.txt", "w");
    if (h) { fputs("ok", h); fclose(h); }
    printf("done\n");
    return 0;
}
"""

ABS_TARGET = Path("/tmp/pwned_by_sandbox_test")


def test_fs_escape_blocked(executor, c_binary, limits, requires_landlock, tmp_path):
    # Pre-clean in case a previous broken run left debris.
    for p in (ABS_TARGET, tmp_path / "escape"):
        try:
            p.unlink()
        except OSError:
            pass

    binary = c_binary("ag_escape", ESCAPE_SRC)
    result = executor.run([binary], "", limits)

    assert result.exit_code == 0, f"program itself should run fine: {result!r}"
    assert "done" in result.stdout
    assert result.enforcement.filesystem_hardened is True
    # Absolute-path write outside the run dir: blocked.
    assert not ABS_TARGET.exists(), "absolute /tmp write escaped the sandbox!"
    # Relative ../../escape from the run dir: blocked.
    # run_dir = <base>/ag_run_XXX  ->  ../.. = parent of base
    escape_path = Path(result.enforcement.temp_dir).parent.parent / "escape"
    assert not escape_path.exists(), f"relative escape write landed at {escape_path}"
    # Run dir cleaned up afterwards.
    assert not os.path.exists(result.enforcement.temp_dir)
