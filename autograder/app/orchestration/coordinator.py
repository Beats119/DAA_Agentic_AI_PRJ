"""Agent coordinator: runs analyzers in subprocesses with HARD deadlines.

DESIGN (D5): the previous thread-pool coordinator could not enforce its
per-analyzer timeout — ``as_completed()`` was iterated without a timeout
and ``future.result(timeout=...)`` was called on already-finished
futures, and Python threads cannot be killed anyway, so one hung
analyzer hung the whole evaluation (and its sandbox children) forever.

Now every analyzer runs in its own worker *process*
(``python -m autograder.app.orchestration.worker``, ``start_new_session``)
driven by a poll loop:

- at most ``max_parallel`` workers run concurrently;
- each worker has a per-analyzer deadline (``AnalyzerJob.timeout_s``):
  on expiry the coordinator sends SIGTERM to the worker's process group
  (the worker's own SIGTERM handler kills its descendant tree — sandbox
  launchers are separate session leaders), waits a short grace period,
  then SIGKILLs the group;
- an overall evaluation deadline (``evaluation_timeout_s``, default
  1800s) bounds the whole phase: running workers are terminated the
  same way and never-started jobs are recorded as skipped — the
  evaluation still finishes and produces its report;
- a worker that crashes, exits without a result envelope, or produces a
  malformed envelope degrades to a ``failed`` result with diagnostics
  (exit code, bounded stderr tail). Analyzer faults NEVER raise here.

TIMEOUT SEMANTICS: a timed-out analyzer yields status ``failed`` with
``diagnostics["timed_out"] = True`` and NO score — the scoring engine
treats it as missing evidence under the rules' uncertainty policy. A
timeout is never a zero.

DETERMINISM: outcomes are keyed by metric and evidence is returned per
job; the workflow appends evidence to the run registry in job (rules)
order, so completion order never affects the assembled report.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..analyzers.base import build_result
from ..core.schemas import AnalyzerResult, Evidence, MetricConfig

log = logging.getLogger("autograder.workflow")

DEFAULT_ANALYZER_TIMEOUT_S = 180.0
DEFAULT_EVALUATION_TIMEOUT_S = 1800.0
_TERMINATE_GRACE_S = 2.0
_POLL_INTERVAL_S = 0.05
_STDERR_TAIL_CHARS = 4000

WORKER_MODULE = "autograder.app.orchestration.worker"


@dataclass
class AnalyzerJob:
    """One unit of analyzer work for the coordinator.

    ``module_path`` names the analyzer module (it must expose
    ``analyze(ctx) -> AnalyzerResult``); ``ctx`` is the parent-side
    :class:`AnalyzerContext`, serialized via ``ctx.to_spec()`` and
    rebuilt inside the worker. ``gateway_spec`` names the gateway
    factory the worker should construct (see ``worker.py``); None means
    the analyzer runs without a gateway.
    """

    metric_name: str
    analyzer_id: str
    module_path: str
    ctx: Any  # AnalyzerContext
    timeout_s: float = DEFAULT_ANALYZER_TIMEOUT_S
    gateway_spec: dict[str, Any] | None = None


@dataclass
class AnalyzerOutcome:
    """One analyzer's result plus the evidence it minted in the worker."""

    result: AnalyzerResult
    evidence: list[Evidence] = field(default_factory=list)
    budget: dict[str, int] | None = None  # worker's final budget counters


def _failed_result(
    job: AnalyzerJob,
    reason: str,
    detail: str = "",
    *,
    timed_out: bool = False,
    extra_diagnostics: dict[str, Any] | None = None,
) -> AnalyzerResult:
    # ``metric`` is only used for the name here; the real MetricConfig is
    # unavailable in the failure path, so build a minimal stand-in.
    metric = MetricConfig(
        name=job.metric_name, analyzer=job.analyzer_id, weight=0.0
    )
    diagnostics: dict[str, Any] = {"reason": reason}
    if timed_out:
        diagnostics["timed_out"] = True
    if extra_diagnostics:
        diagnostics.update(extra_diagnostics)
    if detail:
        diagnostics["detail"] = detail
    return build_result(
        metric=metric,
        analyzer_id=job.analyzer_id,
        status="failed",
        diagnostics=diagnostics,
        warnings=[f"analyzer {job.analyzer_id!r} failed: {reason}"],
    )


def _skipped_result(job: AnalyzerJob, reason: str) -> AnalyzerResult:
    metric = MetricConfig(
        name=job.metric_name, analyzer=job.analyzer_id, weight=0.0
    )
    return build_result(
        metric=metric,
        analyzer_id=job.analyzer_id,
        status="skipped",
        diagnostics={"reason": reason},
        warnings=[f"analyzer {job.analyzer_id!r} skipped: {reason}"],
    )


def _worker_env() -> dict[str, str]:
    """Environment for worker processes.

    Inherits the parent environment and pins PYTHONPATH to the parent's
    ``sys.path`` so the worker imports exactly the same code (including,
    in tests, test-double factories named by dotted path).
    """
    env = dict(os.environ)
    paths = [p for p in sys.path if p]
    if paths:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            paths + ([existing] if existing else []))
    return env


@dataclass
class _Running:
    job: AnalyzerJob
    proc: subprocess.Popen
    started: float  # monotonic, for duration reporting
    deadline: float  # monotonic
    spec_path: Path
    result_path: Path
    stderr_path: Path
    kill_at: float | None = None  # SIGKILL escalation time, once TERM sent
    timeout_reason: str | None = None  # set once termination has begun
    timed_out: bool = False  # per-analyzer deadline (vs overall deadline)


class AnalyzerCoordinator:
    """Subprocess-per-analyzer coordinator with enforceable deadlines."""

    def __init__(
        self,
        *,
        max_parallel: int = 4,
        evaluation_timeout_s: float = DEFAULT_EVALUATION_TIMEOUT_S,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be >= 1")
        if evaluation_timeout_s <= 0:
            raise ValueError("evaluation_timeout_s must be > 0")
        self.max_parallel = int(max_parallel)
        self.evaluation_timeout_s = float(evaluation_timeout_s)

    # ------------------------------------------------------------------ #
    def run(
        self, jobs: list[AnalyzerJob]
    ) -> dict[str, AnalyzerOutcome]:
        """Run all jobs; return ``{metric_name: AnalyzerOutcome}``.

        Never raises for analyzer faults. Bounded in wall time by
        ``evaluation_timeout_s`` plus termination grace.
        """
        outcomes: dict[str, AnalyzerOutcome] = {}
        if not jobs:
            return outcomes
        overall_deadline = time.monotonic() + self.evaluation_timeout_s
        workdir = Path(tempfile.mkdtemp(prefix="ag_coord_"))
        running: list[_Running] = []
        try:
            pending: list[AnalyzerJob] = list(jobs)
            while pending or running:
                now = time.monotonic()

                # Overall deadline: stop starting work; terminate runners.
                if pending and now >= overall_deadline:
                    for job in pending:
                        log.info("analyzer %s: skipped (evaluation deadline "
                                 "exceeded before it started)", job.metric_name)
                        outcomes[job.metric_name] = AnalyzerOutcome(
                            result=_skipped_result(
                                job,
                                "evaluation deadline exceeded "
                                f"({self.evaluation_timeout_s:.0f}s); "
                                "analyzer was not started"),
                        )
                    pending = []

                # Start workers up to the parallelism bound.
                while (pending and len(running) < self.max_parallel
                       and time.monotonic() < overall_deadline):
                    job = pending.pop(0)
                    running.append(self._start(job, workdir,
                                               overall_deadline))

                # Poll runners: reap finished, enforce deadlines.
                still: list[_Running] = []
                for entry in running:
                    outcome = self._poll(entry, overall_deadline)
                    if outcome is None:
                        still.append(entry)
                    else:
                        outcomes[entry.job.metric_name] = outcome
                running = still

                if running or pending:
                    time.sleep(_POLL_INTERVAL_S)
            return outcomes
        finally:
            # No worker outlives the coordinator, even on internal error.
            for entry in running:
                _terminate(entry.proc, force=True)
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def _start(
        self, job: AnalyzerJob, workdir: Path, overall_deadline: float
    ) -> _Running:
        job_dir = workdir / _safe_name(job.metric_name)
        job_dir.mkdir(parents=True, exist_ok=True)
        spec_path = job_dir / "spec.json"
        result_path = job_dir / "result.json"
        stderr_path = job_dir / "stderr.log"
        spec = {
            "module_path": job.module_path,
            "context": job.ctx.to_spec(job.gateway_spec),
        }
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        stderr_fh = open(stderr_path, "wb")  # noqa: SIM115 - handed to Popen
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", WORKER_MODULE,
                 str(spec_path), str(result_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_fh,
                start_new_session=True,
                env=_worker_env(),
            )
        finally:
            stderr_fh.close()
        deadline = min(
            time.monotonic() + max(0.1, float(job.timeout_s)),
            overall_deadline,
        )
        return _Running(
            job=job, proc=proc, started=time.monotonic(), deadline=deadline,
            spec_path=spec_path, result_path=result_path,
            stderr_path=stderr_path,
        )

    # ------------------------------------------------------------------ #
    def _finish(self, entry: _Running, rc: int | None) -> AnalyzerOutcome:
        """Collect a finished worker's outcome and log one summary line."""
        outcome = self._collect(entry, rc)
        log.info("analyzer %s: %s in %.1fs",
                 entry.job.metric_name, outcome.result.status,
                 time.monotonic() - entry.started)
        return outcome

    def _poll(
        self, entry: _Running, overall_deadline: float
    ) -> AnalyzerOutcome | None:
        """Return the job's outcome if finished/terminated, else None."""
        now = time.monotonic()
        rc = entry.proc.poll()
        if rc is not None:
            return self._finish(entry, rc)
        if entry.kill_at is not None:
            if now >= entry.kill_at:
                _terminate(entry.proc, force=True)
                entry.proc.wait()
                return self._finish(entry, entry.proc.returncode)
            return None
        if now >= entry.deadline:
            timed_out = entry.deadline < overall_deadline
            reason = (
                f"analyzer timed out after {entry.job.timeout_s:.0f}s"
                if timed_out else
                "evaluation deadline exceeded "
                f"({self.evaluation_timeout_s:.0f}s); analyzer terminated"
            )
            _terminate(entry.proc, force=False)
            entry.kill_at = now + _TERMINATE_GRACE_S
            entry.timeout_reason = reason
            entry.timed_out = timed_out
            return None
        return None

    # ------------------------------------------------------------------ #
    def _collect(self, entry: _Running, rc: int | None) -> AnalyzerOutcome:
        job = entry.job
        stderr_tail = _read_tail(entry.stderr_path)
        if entry.timeout_reason is not None:
            detail = f"worker exit code: {rc}"
            if stderr_tail:
                detail += f"\nstderr tail:\n{stderr_tail}"
            return AnalyzerOutcome(result=_failed_result(
                job, entry.timeout_reason, detail,
                timed_out=entry.timed_out,
                extra_diagnostics={"exit_code": rc},
            ))
        envelope = _load_envelope(entry.result_path)
        if envelope is None:
            reason = ("analyzer worker exited without a result "
                      f"(exit code {rc})")
            return AnalyzerOutcome(result=_failed_result(
                job, reason, stderr_tail,
                extra_diagnostics={"exit_code": rc},
            ))
        if not envelope.get("ok"):
            reason = (f"analyzer raised {envelope.get('error_type', 'Error')}: "
                      f"{envelope.get('message', '')}")
            return AnalyzerOutcome(result=_failed_result(
                job, reason, str(envelope.get("traceback", ""))[-2000:],
                extra_diagnostics={"exit_code": rc},
            ))
        try:
            result = AnalyzerResult.model_validate(envelope["result"])
            evidence = [Evidence.model_validate(e)
                        for e in envelope.get("evidence", [])]
        except Exception as exc:  # noqa: BLE001 - malformed envelope guard
            return AnalyzerOutcome(result=_failed_result(
                job, f"malformed analyzer result envelope: {exc}",
                stderr_tail, extra_diagnostics={"exit_code": rc},
            ))
        budget = envelope.get("budget")
        return AnalyzerOutcome(
            result=result, evidence=evidence,
            budget=budget if isinstance(budget, dict) else None,
        )


def _safe_name(metric_name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_"
                   for c in metric_name) or "job"


def _read_tail(path: Path, limit: int = _STDERR_TAIL_CHARS) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def _load_envelope(path: Path) -> dict[str, Any] | None:
    """Parse a worker result envelope; None if missing/malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _terminate(proc: subprocess.Popen, *, force: bool) -> None:
    """Terminate a worker's process group (TERM, or KILL when forced)."""
    if proc.poll() is not None:
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass


def run_analyzers(
    jobs: list[AnalyzerJob],
    max_workers: int = 4,
    evaluation_timeout_s: float = DEFAULT_EVALUATION_TIMEOUT_S,
) -> dict[str, AnalyzerOutcome]:
    """Run analyzer jobs in worker processes; never raise for analyzer faults.

    Returns ``{metric_name: AnalyzerOutcome}``. A job that raises, crashes,
    times out, or returns garbage yields a ``failed`` (or ``skipped``)
    result with diagnostics — the run continues, and the whole call is
    bounded by ``evaluation_timeout_s``.
    """
    coordinator = AnalyzerCoordinator(
        max_parallel=max_workers,
        evaluation_timeout_s=evaluation_timeout_s,
    )
    return coordinator.run(jobs)
