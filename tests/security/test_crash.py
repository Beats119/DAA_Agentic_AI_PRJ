"""Segfault: signal recorded, not a hang, no timeout."""

import signal

CRASH_SRC = r"""
int main() { int *p = 0; *p = 42; return 0; }
"""


def test_segfault_recorded(executor, c_binary, limits):
    binary = c_binary("ag_crash", CRASH_SRC)
    result = executor.run([binary], "", limits)

    assert result.timed_out is False
    assert result.signal == signal.SIGSEGV, f"expected SIGSEGV, got {result!r}"
    assert result.exit_code is None
    assert result.wall_s < limits.wall_time_s
