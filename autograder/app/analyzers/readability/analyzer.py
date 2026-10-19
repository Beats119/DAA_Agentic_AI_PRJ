"""Readability analyzer: LLM rubric review with deterministic aggregation.

The local model rates the source on fixed rubric dimensions (naming,
comments, formatting, structure, safety_hygiene), each 0-1 with cited
evidence, via the versioned ``prompts/code_review.txt``. Aggregation is
fully deterministic: mean of the dimension scores, quantized to the nearest
0.05.

Statuses:
- ``completed`` — validated structured response received; score is the
  quantized mean.
- ``inconclusive`` — gateway unavailable/unreachable, or the model failed
  structured validation after retries. Score is None with an explicit reason.

The LLM score is ADVISORY: it never overrides deterministic analyzer
results. No score is ever produced without a validated structured response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from ...core.schemas import StrictModel
from ...ollama.models import model_cfg_for
from ...ollama.prompts import load_prompt
from ..base import AnalyzerContext, build_result

ANALYZER_ID = "readability"

DEFAULTS = {
    "quantization_step": 0.05,
    "max_source_chars": 12000,
}

CriterionName = Literal["naming", "comments", "formatting", "structure", "safety_hygiene"]


class ReviewCriterion(StrictModel):
    name: CriterionName
    score_0_1: float
    evidence: str = ""
    explanation: str = ""


class CodeReview(StrictModel):
    criteria: list[ReviewCriterion]
    suggestions: list[str] = []
    confidence: float = 0.0
    overall_note: str = ""


def _quantize(value: float, step: float) -> float:
    """Round to the nearest multiple of ``step`` (bounded quantization)."""
    q = round(value / step) * step
    return max(0.0, min(1.0, round(q, 4)))


def _aggregate(review: CodeReview, step: float) -> tuple[float, dict[str, float]]:
    by_name = {c.name: c.score_0_1 for c in review.criteria}
    mean = sum(by_name.values()) / len(by_name) if by_name else 0.0
    return _quantize(mean, step), {k: round(v, 3) for k, v in by_name.items()}


def analyze(ctx: AnalyzerContext) -> Any:
    params = {**DEFAULTS, **ctx.metric.params}

    if ctx.gateway is None:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            diagnostics={"reason": "no LLM gateway configured"},
            warnings=["readability review needs the local model; none was provided"],
        )
    try:
        source = Path(ctx.source_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="failed",
            diagnostics={"reason": f"could not read source: {exc}"},
        )

    try:
        front_matter, body = load_prompt("code_review")
    except Exception as exc:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            diagnostics={"reason": f"prompt template unavailable: {exc}"},
        )

    prompt = body.replace(
        "{source_code}", source[: int(params["max_source_chars"])]
    )
    try:
        model_cfg = model_cfg_for("code_review")
    except Exception as exc:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            diagnostics={"reason": f"model config unavailable: {exc}"},
        )

    try:
        review, record = ctx.gateway.generate_structured(
            task="code_review",
            prompt=prompt,
            schema=CodeReview,
            model_cfg=model_cfg,
            prompt_name="code_review",
        )
    except Exception as exc:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            diagnostics={
                "reason": "LLM review unavailable or failed structured validation",
                "detail": str(exc)[:300],
                "prompt_version": front_matter.get("version"),
            },
            warnings=[
                "readability could not be assessed — the local model was "
                "unreachable or did not return valid structured output"
            ],
        )

    score, by_dimension = _aggregate(review, float(params["quantization_step"]))
    evidence_refs = [
        ctx.add_evidence(
            ANALYZER_ID,
            "llm_review",
            {
                "dimensions": by_dimension,
                "quantized_score": score,
                "quantization_step": params["quantization_step"],
                "suggestions": review.suggestions,
                "model_confidence": review.confidence,
                "overall_note": review.overall_note,
                "criteria": [c.model_dump() for c in review.criteria],
                "generation": record.model_dump(),
                "prompt_version": front_matter.get("version"),
            },
            confidence=float(review.confidence) if 0 <= review.confidence <= 1 else None,
        )
    ]

    findings = [
        f"{c.name}: {c.score_0_1:.2f} — {c.explanation or c.evidence}"
        for c in review.criteria
    ] + [f"suggestion: {s}" for s in review.suggestions]

    return build_result(
        metric=ctx.metric,
        analyzer_id=ANALYZER_ID,
        status="completed",
        score=score,
        measurements={
            "dimensions": by_dimension,
            "quantized_score": score,
            "model_confidence": review.confidence,
        },
        evidence_refs=evidence_refs,
        findings=findings,
        limitations=[
            "LLM review is advisory and subjective; it never overrides "
            "deterministic analyzer results"
        ],
        execution_metadata={
            "model": record.model,
            "prompt_version": front_matter.get("version"),
        },
    )
