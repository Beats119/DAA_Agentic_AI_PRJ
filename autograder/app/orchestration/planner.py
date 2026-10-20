"""Evaluation planner: turns (spec, rules) into an ordered, dependency-aware plan.

The planner is fully deterministic and LLM-free. It does not execute
anything; it describes *what* the workflow will do so the coordinator and
the run record can reason about it. Steps mirror the run-state machine in
``autograder.app.persistence.store``.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

# DECISION: analyzer modules are resolved through this registry (not by
# stringly-typed imports scattered through the workflow) so availability
# probing happens in exactly one place.
ANALYZER_MODULES: dict[str, str] = {
    "correctness": "autograder.app.analyzers.correctness.analyzer",
    "compiler": "autograder.app.analyzers.compiler.analyzer",
    "static_analysis": "autograder.app.analyzers.static_analysis.analyzer",
    "modularity": "autograder.app.analyzers.modularity.analyzer",
    "complexity": "autograder.app.analyzers.complexity.analyzer",
    "memory_safety": "autograder.app.analyzers.memory_safety.analyzer",
    "readability": "autograder.app.analyzers.readability.analyzer",
    "similarity": "autograder.app.analyzers.similarity.analyzer",
}

# Analyzers that may run concurrently once compilation succeeded.
# ``correctness`` is excluded: it needs oracle-assessed tests first.
PARALLEL_ANALYZERS: tuple[str, ...] = (
    "compiler",
    "static_analysis",
    "modularity",
    "complexity",
    "memory_safety",
    "similarity",
    "readability",
)


@dataclass(frozen=True)
class PlanStep:
    name: str
    kind: str  # "llm" | "deterministic" | "hybrid"
    dependencies: tuple[str, ...] = ()
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class EvaluationPlan:
    steps: tuple[PlanStep, ...]
    analyzer_ids: tuple[str, ...]  # metrics' analyzers, in rules order
    analyzer_available: dict[str, bool] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def step(self, name: str) -> PlanStep:
        for s in self.steps:
            if s.name == name:
                return s
        raise KeyError(f"no plan step {name!r}")


def _probe_analyzer(analyzer_id: str) -> tuple[bool, str | None]:
    module = ANALYZER_MODULES.get(analyzer_id)
    if module is None:
        return False, f"unknown analyzer id {analyzer_id!r}"
    try:
        importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 - availability probe, report only
        return False, f"analyzer {analyzer_id!r} failed to import: {exc}"
    return True, None


def build_plan(spec: Any, rules: Any) -> EvaluationPlan:
    """Build the ordered evaluation plan for ``(spec, rules)``.

    ``spec`` may be None (spec extraction unavailable) — the plan then marks
    LLM-dependent steps as degraded instead of failing.
    """
    warnings: list[str] = []
    analyzer_available: dict[str, bool] = {}
    analyzer_ids: list[str] = []

    for metric in rules.metrics:
        analyzer_ids.append(metric.analyzer)
        ok, reason = _probe_analyzer(metric.analyzer)
        analyzer_available[metric.name] = ok
        if not ok:
            msg = (
                f"metric {metric.name!r}: analyzer {metric.analyzer!r} "
                f"unavailable ({reason}); "
                + (
                    "required metric — the run will be marked incomplete, "
                    "never silently scored"
                    if metric.required
                    else "optional metric — handled per uncertainty policy"
                )
            )
            warnings.append(msg)

    spec_degraded = spec is None
    if spec_degraded:
        warnings.append(
            "no specification available: LLM test generation is impossible; "
            "correctness will be inconclusive and the run will carry "
            "completed_with_warnings (or incomplete for required metrics)"
        )

    steps = (
        PlanStep("ingest", "deterministic", (),
                 True, "read + hash the assignment document"),
        PlanStep("validate_rules", "deterministic", ("ingest",),
                 True, "load and validate the versioned rules config"),
        PlanStep("extract_spec", "llm", ("validate_rules",),
                 False, "specification agent extracts requirements/examples"),
        PlanStep("compile", "deterministic", ("validate_rules",),
                 True, "compile the student submission in the sandbox"),
        PlanStep("generate_tests", "llm", ("extract_spec",),
                 False, "fresh LLM candidate tests, bounded refinement"),
        PlanStep("oracle_assess", "hybrid", ("generate_tests",),
                 False, "tiered oracle verdicts for every candidate"),
        PlanStep("static_analyzers", "deterministic", ("compile",),
                 True, "parallel analyzer group (compiler, static, modularity, "
                       "complexity, memory safety, similarity, readability)"),
        PlanStep("correctness", "deterministic", ("compile", "oracle_assess"),
                 False, "run trusted tests against the student binary"),
        PlanStep("score", "deterministic",
                 ("static_analyzers", "correctness"),
                 True, "deterministic weighted scoring from validated evidence"),
        PlanStep("feedback", "llm", ("score",),
                 False, "feedback generation + deterministic verification "
                        "(bounded retry, evidence-based fallback)"),
        PlanStep("report", "deterministic", ("feedback",),
                 True, "build, validate and persist JSON report + artifacts"),
    )
    return EvaluationPlan(
        steps=steps,
        analyzer_ids=tuple(analyzer_ids),
        analyzer_available=analyzer_available,
        warnings=tuple(warnings),
    )
