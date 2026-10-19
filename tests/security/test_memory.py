"""Memory bomb: RLIMIT_AS must kill it; the host must be unaffected."""

import signal

MEMBOMB_SRC = r"""
#include <stdlib.h>
#include <stdio.h>
int main() {
    size_t n = 512UL * 1024 * 1024;   /* 512MB > 256MB RLIMIT_AS */
    volatile char *p = (volatile char *)malloc(n);
    /* No NULL check: a hostile bomb won't have one. With RLIMIT_AS=256MB the
       512MB mmap fails -> malloc returns NULL -> first store segfaults. */
    for (size_t i = 0; i < n; i += 4096) p[i] = 1;
    printf("%d\n", p[0]);
    return 0;
}
"""


def test_memory_bomb_contained(executor, c_binary, limits):
    assert limits.memory_mb == 256
    binary = c_binary("ag_membomb", MEMBOMB_SRC)
    result = executor.run([binary], "", limits)

    assert result.timed_out is False, "bomb should die from the address-space limit, not the wall clock"
    assert result.signal is not None, f"expected a fatal signal, got {result!r}"
    assert result.signal == signal.SIGSEGV, f"expected SIGSEGV from RLIMIT_AS, got {result.signal}"
    assert result.exit_code is None
    assert result.wall_s < limits.wall_time_s, "should die quickly, not via timeout"
    assert result.enforcement.resource_limits is True
    # Host unaffected: we are alive and can still allocate.
    assert bytearray(1024 * 1024) is not None
