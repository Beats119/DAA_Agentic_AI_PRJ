"""Evaluation planner, agent coordinator, evidence registry, feedback, workflow.

Phase 6 orchestration: the flow diagram as code.
"""

from .coordinator import (
    AnalyzerCoordinator,
    AnalyzerJob,
    AnalyzerOutcome,
    run_analyzers,
)
from .evidence import EvidenceError, EvidenceRegistry
from .feedback import (
    DEFAULT_FEEDBACK_RETRIES,
    FeedbackFailure,
    FeedbackText,
    VerificationResult,
    build_trusted_results,
    deterministic_fallback,
    finalize_feedback,
    generate_feedback,
    verify_feedback,
)
from .planner import (
    ANALYZER_MODULES,
    EvaluationPlan,
    PlanStep,
    build_plan,
)
from .run import main, make_gateway, run_evaluation
from .workflow import RunResult, WorkflowError, _Abort, evaluate

__all__ = [
    "ANALYZER_MODULES",
    "DEFAULT_FEEDBACK_RETRIES",
    "AnalyzerCoordinator",
    "AnalyzerJob",
    "AnalyzerOutcome",
    "EvaluationPlan",
    "EvidenceError",
    "EvidenceRegistry",
    "FeedbackFailure",
    "FeedbackText",
    "PlanStep",
    "RunResult",
    "VerificationResult",
    "WorkflowError",
    "_Abort",
    "build_plan",
    "build_trusted_results",
    "deterministic_fallback",
    "evaluate",
    "finalize_feedback",
    "generate_feedback",
    "make_gateway",
    "run_analyzers",
    "run_evaluation",
    "verify_feedback",
]
