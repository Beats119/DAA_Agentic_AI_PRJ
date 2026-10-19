"""Output flood: stdout must be capped, never buffered unbounded."""

FLOOD_SRC = r"""
#include <stdio.h>
int main() {
    while (1) printf("0123456789ABCDEF\n");
    return 0;
}
"""


def test_output_flood_capped(executor, c_binary):
    from autograder.app.sandbox.executor import ExecLimits

    cap = 65536
    limits = ExecLimits(wall_time_s=5.0, cpu_time_s=10.0, memory_mb=256, output_bytes=cap)
    binary = c_binary("ag_flood", FLOOD_SRC)
    result = executor.run([binary], "", limits)

    assert result.output_truncated is True
    assert len(result.stdout.encode("utf-8", errors="replace")) <= cap
    assert len(result.stderr.encode("utf-8", errors="replace")) <= cap
    # The run finished (via timeout kill) instead of OOMing the test process.
    assert result.timed_out is True
