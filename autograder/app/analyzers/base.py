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
