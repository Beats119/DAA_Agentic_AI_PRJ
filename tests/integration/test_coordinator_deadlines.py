"""Phase E regression tests: coordinator deadlines are REAL (D5).

The old thread-pool coordinator iterated ``as_completed()`` without a
timeout, so one hung analyzer hung the whole evaluation forever. These
tests run test-only analyzers (``tests.integration.deadline_analyzers``)
through the production mechanism — one worker subprocess per analyzer —
and assert hard wall-clock bounds, honest non-zero failure records, and
that no sandboxed child process is orphaned by a deadline kill.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from autograder.app.analyzers.base import AnalyzerContext
from autograder.app.core.schemas import MetricConfig
from autograder.app.orchestration.coordinator import (
    AnalyzerJob,
    run_analyzers,
)
from autograder.app.rules.loader import load_rules

DL = "tests.integration.deadline_analyzers"


def _rules():
    repo = Path(__file__).resolve().parents[2]
    return load_rules(repo / "examples" / "rules" / "add.json")


def _ctx(tmp_path: Path, metric_name: str, executor=None) -> AnalyzerContext:
    return AnalyzerContext(
        source_path=str(tmp_path / "submission.c"),
        binary_path=None,
        sanitizer_binary_path=None,
        rules=_rules(),
        metric=MetricConfig(name=metric_name, analyzer="test_only",
                            weight=0.5),
        run_dir=str(tmp_path),
        executor=executor,
        compiler=None,
        run_id="deadline_test",
    )


def _job(tmp_path: Path, metric: str, fn: str, timeout_s: float,
         executor=None) -> AnalyzerJob:
    return AnalyzerJob(
        metric_name=metric,
        analyzer_id="test_only",
        module_path=f"{DL}:{fn}",
        ctx=_ctx(tmp_path, metric, executor=executor),
        timeout_s=timeout_s,
    )


def test_no_jobs_is_empty():
    assert run_analyzers([]) == {}


def test_hanging_analyzer_is_terminated_at_its_deadline(tmp_path):
    """D5 core regression: hang + fast jobs; the hang cannot block the run."""
    jobs = [
        _job(tmp_path, "hang_metric", "hang_analyze", timeout_s=3.0),
        _job(tmp_path, "fast_metric", "fast_analyze", timeout_s=30.0),
    ]
    started = time.monotonic()
    outcomes = run_analyzers(jobs, max_workers=2)
    elapsed = time.monotonic() - started

    # 3s deadline + termination grace + startup slack — never ~600s.
    assert elapsed < 15.0
    hang = outcomes["hang_metric"].result
    assert hang.status == "failed"
    assert hang.score is None  # missing evidence, NEVER a zero
    assert hang.diagnostics.get("timed_out") is True
    assert "timed out" in hang.diagnostics["reason"]
    # The well-behaved analyzer's real result is unaffected.
    fast = outcomes["fast_metric"].result
    assert fast.status == "completed"
    assert fast.score == pytest.approx(0.9)


def test_crashing_analyzer_fails_with_diagnostics(tmp_path):
    """os._exit(3) in the worker -> failed result citing the exit code."""
    jobs = [
        _job(tmp_path, "crash_metric", "crash_analyze", timeout_s=30.0),
        _job(tmp_path, "fast_metric", "fast_analyze", timeout_s=30.0),
    ]
    started = time.monotonic()
    outcomes = run_analyzers(jobs, max_workers=2)
    assert time.monotonic() - started < 15.0

    crash = outcomes["crash_metric"].result
    assert crash.status == "failed"
    assert crash.score is None
    assert crash.diagnostics.get("exit_code") == 3
    assert "without a result" in crash.diagnostics["reason"]
    assert outcomes["fast_metric"].result.status == "completed"


def test_malformed_analyzers_fail_and_evaluation_completes(tmp_path):
    """Raising, wrong-type, and unimportable analyzers all degrade to failed."""
    jobs = [
        _job(tmp_path, "raise_metric", "raise_analyze", timeout_s=30.0),
        _job(tmp_path, "wrong_metric", "wrong_type_analyze", timeout_s=30.0),
        AnalyzerJob(
            metric_name="missing_metric",
            analyzer_id="test_only",
            module_path="tests.integration.no_such_module_xyz",
            ctx=_ctx(tmp_path, "missing_metric"),
            timeout_s=30.0,
        ),
    ]
    outcomes = run_analyzers(jobs, max_workers=3)
    assert set(outcomes) == {"raise_metric", "wrong_metric",
                             "missing_metric"}
    for outcome in outcomes.values():
        assert outcome.result.status == "failed"
        assert outcome.result.score is None
    assert "RuntimeError" in outcomes["raise_metric"].result.diagnostics[
        "reason"]
    assert "not AnalyzerResult" in outcomes["wrong_metric"].result.diagnostics[
        "reason"]


def test_malformed_envelope_file_is_rejected(tmp_path):
    """The coordinator's envelope parser refuses garbage (unit-level)."""
    from autograder.app.orchestration.coordinator import _load_envelope

    garbage = tmp_path / "result.json"
    garbage.write_text("{not json at all")
    assert _load_envelope(garbage) is None
    garbage.write_text('["a", "list", "is", "not", "an", "envelope"]')
    assert _load_envelope(garbage) is None
    assert _load_envelope(tmp_path / "does_not_exist.json") is None


def test_slow_analyzer_within_deadline_is_used(tmp_path):
    """A 1.5s analyzer under a 30s deadline returns its real result."""
    jobs = [_job(tmp_path, "slow_metric", "slow_analyze", timeout_s=30.0)]
    outcomes = run_analyzers(jobs, max_workers=1)
    result = outcomes["slow_metric"].result
    assert result.status == "completed"
    assert result.score == pytest.approx(0.75)


def test_overall_evaluation_deadline_bounds_everything(tmp_path):
    """All analyzers hang: the overall deadline still finishes the phase."""
    jobs = [
        _job(tmp_path, f"hang_{i}", "hang_analyze", timeout_s=600.0)
        for i in range(3)
    ]
    started = time.monotonic()
    outcomes = run_analyzers(jobs, max_workers=3, evaluation_timeout_s=5.0)
    elapsed = time.monotonic() - started

    assert elapsed < 15.0  # 5s deadline + grace + slack
    assert len(outcomes) == 3
    for outcome in outcomes.values():
        assert outcome.result.status in ("failed", "skipped")
        assert outcome.result.score is None


def test_queued_analyzer_past_overall_deadline_is_skipped(tmp_path):
    """max_parallel=1: the queued job never starts once the deadline hits."""
    jobs = [
        _job(tmp_path, "first_hang", "hang_analyze", timeout_s=600.0),
        _job(tmp_path, "never_started", "fast_analyze", timeout_s=30.0),
    ]
    started = time.monotonic()
    outcomes = run_analyzers(jobs, max_workers=1, evaluation_timeout_s=4.0)
    elapsed = time.monotonic() - started

    assert elapsed < 15.0
    assert outcomes["first_hang"].result.status == "failed"
    skipped = outcomes["never_started"].result
    assert skipped.status == "skipped"
    assert skipped.score is None
    assert "evaluation deadline" in skipped.diagnostics["reason"]


def test_deadline_kill_leaves_no_orphaned_sandbox_child(tmp_path):
    """A killed worker's sandboxed sleep is reaped, not orphaned."""
    from autograder.app.sandbox.executor import SandboxExecutor

    from tests.integration.deadline_analyzers import sleep_597_pids

    executor = SandboxExecutor(base_dir=tmp_path / "sandbox")
    jobs = [_job(tmp_path, "orphan_metric", "orphan_analyze",
                 timeout_s=3.0, executor=executor)]
    started = time.monotonic()
    outcomes = run_analyzers(jobs, max_workers=1)
    assert time.monotonic() - started < 15.0
    assert outcomes["orphan_metric"].result.status == "failed"
    assert outcomes["orphan_metric"].result.diagnostics.get(
        "timed_out") is True

    # The sandboxed sleep really started (proof written by the analyzer).
    assert (tmp_path / "orphan_started.txt").exists()
    # ... and after the deadline kill, it is gone (poll briefly for reaping).
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and sleep_597_pids():
        time.sleep(0.2)
    assert sleep_597_pids() == []
