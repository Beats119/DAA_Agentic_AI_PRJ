"""Fork bomb: RLIMIT_NPROC contains it; killpg reaps the tree."""

import os
import time
import uuid

FORKBOMB_SRC = r"""
#include <unistd.h>
int main() { while (1) fork(); return 0; }
"""


def _proc_cmdline_contains(marker: str) -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode(errors="replace")
            if marker in cmdline:
                with open(f"/proc/{pid}/status") as fh:
                    state = [ln for ln in fh if ln.startswith("State:")]
                if state and "Z (" not in state[0]:
                    return True
        except (OSError, FileNotFoundError):
            continue
    return False


def _wait_until_gone(marker: str, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _proc_cmdline_contains(marker):
            return True
        time.sleep(0.1)
    return not _proc_cmdline_contains(marker)


def test_forkbomb_contained(executor, c_binary):
    from autograder.app.sandbox.executor import ExecLimits

    marker = f"ag_forkbomb_{uuid.uuid4().hex[:8]}"
    limits = ExecLimits(wall_time_s=8.0, cpu_time_s=15.0, memory_mb=256, nproc=32)
    binary = c_binary(marker, FORKBOMB_SRC)
    result = executor.run([binary], "", limits)

    # The executor must return (not hang the test suite).
    assert result.timed_out is True
    # No strays left behind once the group is killed.
    assert _wait_until_gone(marker, timeout_s=5.0), "fork-bomb processes survived"
    # The machine still works.
    assert executor.run(["/bin/true"], "", limits).exit_code == 0
