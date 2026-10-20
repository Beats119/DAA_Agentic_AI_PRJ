"""Agent coordinator: runs analyzers with bounded concurrency.

Each analyzer executes in its own thread with its own
:class:`AnalyzerContext` (so evidence lists are never shared across
threads). A per-analyzer timeout and a catch-all exception guard mean one
misbehaving analyzer degrades to a ``failed`` result — it never crashes
the run.
"""

from __future__ import annotations

import concurrent.futures
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from ..analyzers.base import build_result
from ..core.schemas import AnalyzerResult, MetricConfig


@dataclass
class AnalyzerJob:
    """One unit of analyzer work for the coordinator."""

    metric_name: str
    analyzer_id: str
    analyze_fn: Callable[[Any], AnalyzerResult]
    ctx: Any  # AnalyzerContext
    timeout_s: float = 180.0


DEFAULT_ANALYZER_TIMEOUT_S = 180.0


def _failed_result(job: AnalyzerJob, reason: str, detail: str = "") -> AnalyzerResult:
    # ``metric`` is only used for the name here; the real MetricConfig is
    # unavailable in the failure path, so build a minimal stand-in.
    metric = MetricConfig(
        name=job.metric_name, analyzer=job.analyzer_id, weight=0.0
    )
    diagnostics: dict[str, Any] = {"reason": reason}
    if detail:
        diagnostics["detail"] = detail
    return build_result(
        metric=metric,
        analyzer_id=job.analyzer_id,
        status="failed",
        diagnostics=diagnostics,
        warnings=[f"analyzer {job.analyzer_id!r} failed: {reason}"],
    )


def run_analyzers(
    jobs: list[AnalyzerJob],
    max_workers: int = 4,
) -> dict[str, AnalyzerResult]:
    """Run analyzer jobs concurrently; never raise for analyzer faults.

    Returns ``{metric_name: AnalyzerResult}``. A job that raises or times
    out yields a ``failed`` result with diagnostics — the run continues.
    """
    results: dict[str, AnalyzerResult] = {}
    if not jobs:
        return results

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, min(max_workers, len(jobs))),
        thread_name_prefix="ag_analyzer",
    ) as pool:
        future_to_job = {
            pool.submit(_guarded_analyze, job): job for job in jobs
        }
        for future in concurrent.futures.as_completed(future_to_job):
            job = future_to_job[future]
            try:
                results[job.metric_name] = future.result(
                    timeout=job.timeout_s
                )
            except concurrent.futures.TimeoutError:
                results[job.metric_name] = _failed_result(
                    job,
                    f"analyzer timed out after {job.timeout_s:.0f}s "
                    "(bounded concurrency guard)",
                )
            except Exception as exc:  # noqa: BLE001 - last-resort guard
                results[job.metric_name] = _failed_result(
                    job,
                    f"unexpected coordinator error: {exc}",
                    traceback.format_exc(limit=5),
                )
    return results


def _guarded_analyze(job: AnalyzerJob) -> AnalyzerResult:
    """Run one analyzer; convert exceptions into ``failed`` results."""
    try:
        result = job.analyze_fn(job.ctx)
    except Exception as exc:  # noqa: BLE001 - per-analyzer guard
        return _failed_result(
            job,
            f"analyzer raised {type(exc).__name__}: {exc}",
            traceback.format_exc(limit=8),
        )
    if not isinstance(result, AnalyzerResult):
        return _failed_result(
            job,
            f"analyzer returned {type(result).__name__}, not AnalyzerResult",
        )
    return result
