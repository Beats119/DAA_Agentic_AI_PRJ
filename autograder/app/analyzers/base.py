"""Shared base for all analyzers.

Every analyzer receives an :class:`AnalyzerContext` and returns an
:class:`AnalyzerResult` (see ``autograder.app.core.schemas``). Analyzers must
never see the full run state — only what the context carries.

Status honesty rules (enforced by convention + tests):
- ``completed`` — analysis ran and produced a score.
- ``failed`` — the analyzer itself errored (e.g. student code didn't compile);
  score MUST be None.
- ``skipped`` — not applicable for this submission (recorded reason).
- ``unsupported`` — the required tool/host capability is missing (recorded).
- ``inconclusive`` — analysis ran but evidence is insufficient for a score;
  score MUST be None.

A ``failed``/``inconclusive``/``unsupported`` analyzer NEVER contributes a
silent zero to the grade — the scoring engine treats those per the rules'
uncertainty policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..core.schemas import (
    AnalyzerResult,
    Evidence,
    MetricConfig,
    RuleSet,
    TestCase,
)

ANALYZER_VERSION = "1.0"


@dataclass
class AnalyzerContext:
    """Typed input contract for every analyzer.

    ``test_cases`` carries the validated test cases (with oracle verdicts)
    for execution-based analyzers; source-only analyzers ignore it.
    ``compile_result`` carries the Phase-4 CompileResult for the student
    source when already available (compiler analyzer uses it instead of
    recompiling).
    """

    source_path: str
    binary_path: str | None
    sanitizer_binary_path: str | None
    rules: RuleSet
    metric: MetricConfig
    run_dir: str
    executor: Any  # SandboxExecutor (typed loosely to avoid import cycles)
    compiler: Any  # compile_c function
    gateway: Any | None = None  # OllamaClient or None
    evidence: list[Evidence] = field(default_factory=list)
    run_id: str = "local"
    test_cases: list[TestCase] = field(default_factory=list)
    compile_result: Any | None = None  # CompileResult when available
    # Per-run execution-budget counter (BudgetTracker), shared with the
    # generation/oracle phases (D10). Execution-based analyzers must
    # charge it before each sandbox run; None = no budget wired.
    budget_tracker: Any | None = None
    _evidence_seq: int = field(default=0, repr=False)

    def to_spec(self, gateway_spec: dict[str, Any] | None) -> dict[str, Any]:
        """Serialize this context to plain JSON-safe data for a worker process.

        The analyzer coordinator runs each analyzer in its own subprocess
        (see ``orchestration.worker``); everything the worker needs to
        rebuild an equivalent context travels in this spec. Live objects
        are represented by their construction parameters:

        - ``executor`` -> its base dir / namespace / Landlock flags and the
          sandbox policy dump (the worker constructs a fresh executor);
        - ``gateway`` -> ``gateway_spec`` (a factory reference supplied by
          the caller, since the parent holds the live client);
        - ``compiler`` -> always the production ``compile_c`` entry point;
        - ``budget_tracker`` -> a counter snapshot (the worker returns its
          final counters; the coordinator reconciles the deltas);
        - ``compile_result`` / ``rules`` / ``metric`` / ``test_cases`` ->
          their Pydantic JSON dumps.

        In-process construction (unit tests, direct calls) is unaffected.
        """
        executor = self.executor
        executor_spec: dict[str, Any] | None = None
        if executor is not None:
            policy = getattr(executor, "policy", None)
            executor_spec = {
                "base_dir": str(getattr(executor, "base_dir", "") or ""),
                "use_network_namespace": bool(
                    getattr(executor, "use_network_namespace", True)),
                "use_landlock": bool(getattr(executor, "use_landlock", True)),
                "policy": (policy.model_dump(mode="json")
                           if hasattr(policy, "model_dump") else None),
            }
        tracker = self.budget_tracker
        return {
            "source_path": self.source_path,
            "binary_path": self.binary_path,
            "sanitizer_binary_path": self.sanitizer_binary_path,
            "rules": self.rules.model_dump(mode="json"),
            "metric": self.metric.model_dump(mode="json"),
            "run_dir": self.run_dir,
            "run_id": self.run_id,
            "test_cases": [tc.model_dump(mode="json")
                           for tc in self.test_cases],
            "compile_result": (self.compile_result.model_dump(mode="json")
                               if self.compile_result is not None else None),
            "evidence": [ev.model_dump(mode="json") for ev in self.evidence],
            "evidence_seq": self._evidence_seq,
            "executor": executor_spec,
            "gateway": gateway_spec,
            "budget": (tracker.snapshot()
                       if tracker is not None
                       and hasattr(tracker, "snapshot") else None),
        }

    def add_evidence(
        self,
        analyzer_id: str,
        kind: str,
        data: dict[str, Any],
        *,
        severity: str | None = None,
        confidence: float | None = None,
        affects_grade: bool = True,
    ) -> str:
        """Mint an immutable Evidence record, append it, return its id."""
        self._evidence_seq += 1
        ev = Evidence(
            id=f"ev:{self.run_id}:{self._evidence_seq}",
            analyzer=analyzer_id,
            run_id=self.run_id,
            timestamp=datetime.now(timezone.utc),
            kind=kind,
            data=data,
            severity=severity,
            confidence=confidence,
            affects_grade=affects_grade,
        )
        self.evidence.append(ev)
        return ev.id


def build_result(
    *,
    metric: MetricConfig,
    analyzer_id: str,
    status: str,
    score: float | None = None,
    measurements: dict[str, Any] | None = None,
    evidence_refs: list[str] | None = None,
    findings: list[str] | None = None,
    warnings: list[str] | None = None,
    limitations: list[str] | None = None,
    diagnostics: dict[str, Any] | None = None,
    execution_metadata: dict[str, Any] | None = None,
) -> AnalyzerResult:
    """Construct an AnalyzerResult with sane defaults + status/score guard.

    ``failed``/``inconclusive``/``unsupported``/``skipped`` results must not
    carry a score; ``completed`` SHOULD carry one (None is tolerated but
    recorded as a warning by callers).
    """
    if status in ("failed", "inconclusive", "unsupported", "skipped") and score is not None:
        raise ValueError(
            f"analyzer {analyzer_id}: status {status!r} must not carry a score"
        )
    if score is not None and not 0.0 <= score <= 1.0:
        raise ValueError(f"analyzer {analyzer_id}: score {score!r} outside [0, 1]")
    return AnalyzerResult(
        metric_name=metric.name,
        analyzer_id=analyzer_id,
        analyzer_version=f"{analyzer_id}-{ANALYZER_VERSION}",
        status=status,  # type: ignore[arg-type]
        measurements=measurements or {},
        score=score,
        evidence_refs=evidence_refs or [],
        findings=findings or [],
        warnings=warnings or [],
        limitations=limitations or [],
        diagnostics=diagnostics or {},
        execution_metadata=execution_metadata or {},
    )
