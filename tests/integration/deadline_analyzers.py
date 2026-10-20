"""TEST-ONLY analyzer behaviors for coordinator deadline tests (Phase E).

Each ``*_analyze`` function is referenced from tests by dotted path
(``tests.integration.deadline_analyzers:<name>``) and executed inside a
real coordinator worker subprocess — the same mechanism production
analyzers use. Never imported by production code.
"""

from __future__ import annotations

import os
import threading
import time

from autograder.app.analyzers.base import build_result


def _completed(ctx, score: float = 0.9):
    return build_result(
        metric=ctx.metric,
        analyzer_id=ctx.metric.analyzer,
        status="completed",
        score=score,
        measurements={"test_only": True},
    )


def fast_analyze(ctx):
    """Completes immediately with a real score."""
    return _completed(ctx)


def slow_analyze(ctx):
    """Slow (1.5s) but well within a generous deadline."""
    time.sleep(1.5)
    return _completed(ctx, score=0.75)


def hang_analyze(ctx):
    """Hangs far beyond any test deadline."""
    time.sleep(600)
    return _completed(ctx)  # never reached in deadline tests


def crash_analyze(ctx):
    """Hard-crashes the worker: no result envelope is ever written."""
    os._exit(3)


def raise_analyze(ctx):
    """Raises before producing a result (worker failure envelope path)."""
    raise RuntimeError("simulated analyzer explosion")


def wrong_type_analyze(ctx):
    """Returns garbage instead of an AnalyzerResult (malformed path)."""
    return "not an AnalyzerResult"


def sleep_597_pids() -> list[int]:
    """Pids of live ``sleep 597`` processes (the orphan test's marker)."""
    pids: list[int] = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                parts = fh.read().split(b"\0")
        except OSError:
            continue
        texts = [p.decode(errors="replace") for p in parts if p]
        if "597" in texts and any(t.endswith("sleep") for t in texts):
            pids.append(int(name))
    return pids


def orphan_analyze(ctx):
    """Starts a long-lived SANDBOXED sleep, then hangs.

    The sleep runs through the real sandbox executor (its launcher is a
    separate session leader), so only the worker's SIGTERM descendant
    walk can reap it when the coordinator terminates the worker. Proof
    that the sleep really started is written to ``orphan_started.txt``
    in the run dir — the test asserts on it, so this cannot pass
    vacuously if the sandbox refuses to run sleep.
    """
    from pathlib import Path

    from autograder.app.sandbox.executor import ExecLimits

    def _run_sleep() -> None:
        try:
            ctx.executor.run(
                ["/bin/sleep", "597"], "",
                ExecLimits(wall_time_s=590, memory_mb=256),
            )
        except Exception:
            pass

    threading.Thread(target=_run_sleep, daemon=True).start()
    deadline = time.time() + 15
    while time.time() < deadline:
        if sleep_597_pids():
            Path(ctx.run_dir, "orphan_started.txt").write_text("started")
            break
        time.sleep(0.1)
    time.sleep(600)
    return _completed(ctx)  # never reached in deadline tests
