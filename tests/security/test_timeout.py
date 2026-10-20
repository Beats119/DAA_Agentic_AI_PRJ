"""Infinite loop + forked sleeper: wall timeout must kill the whole tree."""

import os

TIMEOUT_SRC = r"""
#include <stdio.h>
#include <sys/types.h>
#include <unistd.h>
int main() {
    pid_t c = fork();
    if (c == 0) { sleep(30); return 0; }   /* sleeper: proves killpg, not just kill */
    printf("child=%d\n", (int)c);
    fflush(stdout);
    while (1) { }
    return 0;
}
"""


def test_infinite_loop_times_out_and_kills_tree(executor, c_binary, limits):
    binary = c_binary("ag_sleeper", TIMEOUT_SRC)
    result = executor.run([binary], "", limits)

    assert result.timed_out is True, f"expected timeout, got {result!r}"
    assert result.wall_s < limits.wall_time_s + 3.0

    # The forked sleeper must be dead too (whole process group killed).
    # Note: kill(pid, 0) succeeds on zombies, so check /proc state instead.
    child_pid = None
    for line in result.stdout.splitlines():
        if line.startswith("child="):
            child_pid = int(line.split("=")[1])
    assert child_pid, f"sleeper pid not reported; stdout={result.stdout!r}"

    def _sleeper_dead() -> bool:
        try:
            with open(f"/proc/{child_pid}/status") as fh:
                for ln in fh:
                    if ln.startswith("State:"):
                        return "Z (" in ln  # zombie == dead, awaiting reaping
            return True  # no State line: going away
        except (FileNotFoundError, ProcessLookupError):
            return True  # fully reaped

    import time as _time

    deadline = _time.monotonic() + 5.0
    while not _sleeper_dead() and _time.monotonic() < deadline:
        _time.sleep(0.1)
    assert _sleeper_dead(), "forked sleeper survived the timeout: killpg did not kill the tree"

    # Host is still healthy afterwards.
    assert os.getpid() > 0
    assert result.enforcement.process_group_kill is True
    # The per-run temp dir was cleaned up even after the timeout.
    assert not os.path.exists(result.enforcement.temp_dir)
