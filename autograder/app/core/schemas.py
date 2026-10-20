"""Typed contracts for everything that crosses a component boundary.

These Pydantic v2 models are the single source of truth for the shapes of
rules, specifications, test cases, evidence, analyzer results, reports and
run manifests. Components exchange these models — never ``dict`` soup — so
a malformed payload fails loudly at the boundary instead of corrupting a
grade downstream.

Conventions:
- ``extra="forbid"`` on boundary models: unknown fields are rejected.
- Frozen models (``frozen=True``) for anything that must be immutable once
  created (evidence, test cases, analyzer results).
- All datetimes are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .numbers import round_half_up_1

# ---------------------------------------------------------------------------
# Shared aliases
# ---------------------------------------------------------------------------

OracleStatus = Literal["trusted", "provisional", "rejected", "inconclusive"]
AnalyzerStatus = Literal["completed", "failed", "skipped", "unsupported", "inconclusive"]
RequirementKind = Literal["explicit", "inferred", "ambiguous"]
RunStatus = Literal[
    "queued",
    "validating",
    "planning",
    "generating_tests",
    "executing",
    "analyzing",
    "scoring",
    "reporting",
    "completed",
    "completed_with_warnings",
    "failed",
    "incomplete",
]
ReportStatus = Literal["completed", "completed_with_warnings", "incomplete", "failed"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    """Base: forbid unknown fields, validate on assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# ---------------------------------------------------------------------------
# Grading rules
# ---------------------------------------------------------------------------


class Limits(StrictModel):
    time_limit_s: float = Field(gt=0)
    memory_limit_mb: float = Field(gt=0)
    process_limit: int = Field(gt=0, default=32)
    output_limit_bytes: int = Field(gt=0, default=1_000_000)


class CompileConfig(StrictModel):
    compiler: str = "gcc"
    flags: list[str] = Field(default_factory=lambda: ["-O2", "-Wall", "-Wextra"])
    std: str = "c11"


class MetricConfig(StrictModel):
    name: str = Field(min_length=1)
    analyzer: str = Field(min_length=1)
    weight: float = Field(ge=0)
    required: bool = True
    params: dict[str, Any] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)


class ComparisonPolicy(StrictModel):
    whitespace: Literal["exact", "normalize_trailing"] = "exact"
    case_sensitive: bool = True


class TestGenerationPolicy(StrictModel):
    max_rounds: int = Field(ge=1, le=3, default=3)
    max_candidates: int = Field(gt=0, default=24)
    token_budget: int = Field(gt=0, default=20000)
    exec_budget: int = Field(gt=0, default=120)


class OraclePolicy(StrictModel):
    tier_preference: list[Literal["tier1_reference", "tier2_independent", "tier3_llm"]] = Field(
        default_factory=lambda: ["tier1_reference", "tier2_independent", "tier3_llm"]
    )
    reference_solution_path: str | None = None
    expected_outputs_path: str | None = None
    quarantine_after_rounds: int = Field(ge=1, default=3)


class ComplexityRequirement(StrictModel):
    required: str | None = None  # e.g. "O(n log n)"; None = no requirement


class SimilarityPolicy(StrictModel):
    threshold: float = Field(ge=0, le=1, default=0.8)
    corpus_paths: list[str] = Field(default_factory=list)


class UncertaintyPolicy(StrictModel):
    required_metric_missing: Literal["incomplete", "provisional"] = "incomplete"
    optional_metric_missing: Literal["renormalize", "zero", "exclude"] = "renormalize"


class ReportConfig(StrictModel):
    formats: list[Literal["json", "html"]] = Field(default_factory=lambda: ["json", "html"])
    include_source_excerpt: bool = True


class RuleSet(StrictModel):
    """Instructor-editable grading policy. Weights must sum to 1 within 1e-9.

    Set ``normalize_weights: true`` to explicitly opt into rescaling weights
    that do not sum to 1 (recorded in the model + hashed config). Otherwise a
    bad total is rejected — never silently fixed.
    """

    WEIGHT_TOL: ClassVar[float] = 1e-9

    problem_id: str = Field(min_length=1)
    assignment_ref: str = Field(min_length=1)
    version: str = Field(min_length=1)
    limits: Limits
    compile: CompileConfig = Field(default_factory=CompileConfig)
    metrics: list[MetricConfig] = Field(min_length=1)
    comparison_policy: ComparisonPolicy = Field(default_factory=ComparisonPolicy)
    test_generation: TestGenerationPolicy = Field(default_factory=TestGenerationPolicy)
    oracle: OraclePolicy = Field(default_factory=OraclePolicy)
    complexity: ComplexityRequirement = Field(default_factory=ComplexityRequirement)
    similarity: SimilarityPolicy = Field(default_factory=SimilarityPolicy)
    uncertainty_policy: UncertaintyPolicy = Field(default_factory=UncertaintyPolicy)
    report: ReportConfig = Field(default_factory=ReportConfig)
    models: dict[str, str] = Field(default_factory=dict)  # task -> model alias
    normalize_weights: bool = False
    weights_normalized: bool = False

    @model_validator(mode="after")
    def _check_metrics(self) -> "RuleSet":
        names = [m.name for m in self.metrics]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate metric names: {sorted(dupes)}")
        total = sum(m.weight for m in self.metrics)
        if abs(total - 1.0) > self.WEIGHT_TOL:
            if self.normalize_weights and total > 0:
                for m in self.metrics:
                    m.weight = m.weight / total
                self.weights_normalized = True
            else:
                raise ValueError(
                    f"metric weights sum to {total:.6f}, not 1.0 "
                    f"(tolerance {self.WEIGHT_TOL}); set normalize_weights: true "
                    "to opt into rescaling"
                )
        return self


# ---------------------------------------------------------------------------
# Assignment specification
# ---------------------------------------------------------------------------


class RequirementSource(StrictModel):
    page: int | None = None
    excerpt: str = ""


class Requirement(StrictModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: RequirementKind
    source: RequirementSource = Field(default_factory=RequirementSource)


class SpecExample(StrictModel):
    input: str
    output: str
    page: int | None = None


class Ambiguity(StrictModel):
    text: str = Field(min_length=1)
    impact: str = ""  # which metrics/behavior the ambiguity limits


class Specification(StrictModel):
    requirements: list[Requirement] = Field(default_factory=list)
    examples: list[SpecExample] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    extraction_diagnostics: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Assignment document ingestion
# ---------------------------------------------------------------------------


class PageText(StrictModel):
    page_no: int = Field(ge=1)
    text: str


class AssignmentDoc(StrictModel):
    """Result of ingesting an assignment document (PDF or plain text).

    Pure data carrier: produced by the ingestion layer, consumed by the
    specification agent. No LLM involvement.
    """

    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    text: str = Field(min_length=1)
    pages: list[PageText] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Test cases (with tiered oracle provenance)
# ---------------------------------------------------------------------------


class ExpectedProvenance(StrictModel):
    tier: Literal["tier1_reference", "tier2_independent", "tier3_llm", "instructor"]
    method: str = Field(min_length=1)  # how the expected output was established
    detail: str = ""


class ExpectedOutput(StrictModel):
    value: str
    provenance: ExpectedProvenance


# ---------------------------------------------------------------------------
# Oracle verdicts and candidate validation
# ---------------------------------------------------------------------------


class TestCandidate(StrictModel):
    """Raw LLM-proposed test case, before validation and oracle assessment.

    The model proposes ``expected_value`` as a *candidate*; the oracle
    subsystem decides provenance and status. The model must never invent
    provenance or claim a trust status.
    """

    id: str = Field(min_length=1)
    input: str
    input_category: str = Field(min_length=1)
    rationale: str = ""
    requirement_refs: list[str] = Field(default_factory=list)
    expected_value: str


class OracleVerdict(StrictModel):
    """Outcome of assessing one test case's expected output.

    Invariant (enforced by tests, not just convention): a Tier-3
    (LLM-assisted) assessment may NEVER yield status "trusted" on the basis
    of LLM agreement alone. Agreement is supporting evidence only.
    """

    status: OracleStatus
    provenance: ExpectedProvenance
    evidence: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(StrictModel):
    """Deterministic validation of a candidate test case (no LLM)."""

    ok: bool
    errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM generation records
# ---------------------------------------------------------------------------


class GenerationRecord(StrictModel):
    """Provenance for one structured LLM generation call."""

    task: str = Field(min_length=1)
    model: str = Field(min_length=1)
    temperature: float
    seed: int | None = None
    timeout_s: float = Field(gt=0)
    prompt_version: str = Field(min_length=1)
    prompt_hash: str = Field(pattern=r"^sha256:[0-9a-f]{16}$")
    started_at: datetime = Field(default_factory=_utcnow)
    duration_s: float = Field(ge=0)
    # Token usage for this call (all attempts): Ollama's reported
    # eval_count + prompt_eval_count when present, otherwise the gateway's
    # documented estimate. Consumed by the generation budget tracker (D10).
    tokens_used: int = Field(default=0, ge=0)


class TestCase(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    input: str
    input_category: str = Field(min_length=1)
    rationale: str = ""
    requirement_refs: list[str] = Field(default_factory=list)
    expected: ExpectedOutput
    status: OracleStatus = "inconclusive"
    validation_diagnostics: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Evidence (immutable once created)
# ---------------------------------------------------------------------------


class Evidence(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^ev:[A-Za-z0-9_-]+:[0-9]+$")
    analyzer: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    timestamp: datetime = Field(default_factory=_utcnow)
    kind: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    severity: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    affects_grade: bool = True


# ---------------------------------------------------------------------------
# Analyzer results
# ---------------------------------------------------------------------------


class AnalyzerResult(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_name: str = Field(min_length=1)
    analyzer_id: str = Field(min_length=1)
    analyzer_version: str = Field(min_length=1)
    status: AnalyzerStatus
    measurements: dict[str, Any] = Field(default_factory=dict)
    score: float | None = Field(default=None)
    evidence_refs: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    execution_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("score")
    @classmethod
    def _score_range(cls, v: float | None) -> float | None:
        if v is None:
            return v
        import math

        if not math.isfinite(v) or not 0.0 <= v <= 1.0:
            raise ValueError(f"score must be finite and in [0, 1], got {v!r}")
        return v


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


class SubmissionRef(StrictModel):
    id: str
    source_hash: str


class AssignmentRef(StrictModel):
    id: str
    spec_hash: str


class RulesRef(StrictModel):
    version: str
    hash: str


class ReportMetric(StrictModel):
    name: str
    analyzer: str
    # ``weight`` is the EFFECTIVE weight (kept for backward compatibility:
    # it is what ``contribution == 100 * weight * score`` reconciles
    # against). Policy 1.1 additionally exposes both weights explicitly:
    # ``configured_weight`` is the weight from the rules file and
    # ``effective_weight`` the weight actually applied after the
    # missing-evidence policy (renormalization / exclusion). They are
    # optional so reports written before policy 1.1 still validate.
    weight: float = Field(ge=0)
    configured_weight: float | None = Field(default=None, ge=0)
    effective_weight: float | None = Field(default=None, ge=0)
    score: float | None = None
    contribution: float = 0.0
    status: str = ""


class TestStats(StrictModel):
    generated: int = Field(ge=0, default=0)
    validated: int = Field(ge=0, default=0)
    executed: int = Field(ge=0, default=0)
    rejected: int = Field(ge=0, default=0)
    trusted: int = Field(ge=0, default=0)


class FailingTest(StrictModel):
    test_id: str
    input: str
    expected: str
    expected_provenance: ExpectedProvenance
    actual: str | None = None
    note: str = ""


class ModelRecord(StrictModel):
    task: str
    model: str
    temperature: float
    seed: int | None = None
    timeout_s: float
    max_tokens: int
    prompt_version: str


class ReproductionInfo(StrictModel):
    run_id: str
    rules_hash: str
    scoring_policy_version: str
    notes: str = ""


class Report(StrictModel):
    run_id: str = Field(min_length=1)
    submission: SubmissionRef
    assignment: AssignmentRef
    rules: RulesRef
    grade: float | None = Field(default=None)
    status: ReportStatus
    evidence_coverage: dict[str, str | int | float | bool] = Field(default_factory=dict)
    metrics: list[ReportMetric] = Field(default_factory=list)
    analyzer_statuses: dict[str, str] = Field(default_factory=dict)
    test_stats: TestStats = Field(default_factory=TestStats)
    passing_tests: int = Field(ge=0, default=0)
    failing_tests: list[FailingTest] = Field(default_factory=list)
    compiler_diagnostics: list[str] = Field(default_factory=list)
    runtime_measurements: dict[str, Any] = Field(default_factory=dict)
    memory_safety: dict[str, Any] | None = None
    complexity: dict[str, Any] | None = None
    similarity: dict[str, Any] | None = None
    suggestions: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    tool_versions: dict[str, str] = Field(default_factory=dict)
    models: list[ModelRecord] = Field(default_factory=list)
    # Must equal scoring.engine.POLICY_VERSION (single-sourced there; the
    # literal is duplicated here only because core.schemas cannot import
    # the scoring package without a cycle). validate_report enforces the
    # match, so drift fails loudly rather than silently.
    scoring_policy_version: str = "1.1"
    reproduction: ReproductionInfo | None = None
    # DECISION (Phase 6): the verified student feedback text, as plain data.
    # Optional so older reports still validate. The feedback path can never
    # alter the grade or the analyzer evidence — it is evidence rendered
    # into prose, stored here for the report consumer.
    feedback: dict[str, Any] | None = None

    @field_validator("grade")
    @classmethod
    def _grade_bounds(cls, v: float | None) -> float | None:
        if v is None:
            return v
        import math

        if not math.isfinite(v) or not 0.0 <= v <= 100.0:
            raise ValueError(f"grade must be finite and in [0, 100], got {v!r}")
        return round_half_up_1(v)


class RunManifest(StrictModel):
    run_id: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utcnow)
    state: RunStatus
    submission_hash: str
    assignment_hash: str
    rules_hash: str
    rules_version: str
    tool_versions: dict[str, str] = Field(default_factory=dict)
    model_settings: dict[str, Any] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    # See Report.scoring_policy_version: must match scoring.engine.POLICY_VERSION.
    scoring_policy_version: str = "1.1"
