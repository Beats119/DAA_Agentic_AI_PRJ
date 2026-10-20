"""Readability analyzer: deterministic aggregation + honest unavailability.

The LLM itself is faked with a clearly-labeled in-test mock gateway
(production code never touches it); aggregation and quantization are pure
and fully deterministic.
"""

from datetime import datetime, timezone


from conftest import make_analyzer_ctx

from autograder.app.analyzers.readability import analyzer as readability
from autograder.app.analyzers.readability.analyzer import (
    CodeReview,
    _aggregate,
    _quantize,
)
from autograder.app.core.schemas import GenerationRecord


class MockGateway:
    """TEST-ONLY fake Ollama gateway: returns a canned structured review."""

    def __init__(self, review=None, exc=None):
        self._review = review
        self._exc = exc

    def generate_structured(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        record = GenerationRecord(
            task="code_review",
            model="mock-model",
            temperature=0.0,
            seed=42,
            timeout_s=120.0,
            prompt_version="1.0",
            prompt_hash="sha256:" + "0" * 16,
            started_at=datetime.now(timezone.utc),
            duration_s=0.01,
        )
        return self._review, record


def _review(scores):
    criteria = [
        {"name": name, "score_0_1": s, "evidence": f"ev-{name}", "explanation": f"{name} ok"}
        for name, s in scores.items()
    ]
    return CodeReview(
        criteria=criteria,
        suggestions=["rename x"],
        confidence=0.8,
        overall_note="fine",
    )


def test_quantize():
    assert _quantize(0.82, 0.05) == 0.8
    assert _quantize(0.83, 0.05) == 0.85
    assert _quantize(1.0, 0.05) == 1.0
    assert _quantize(0.0, 0.05) == 0.0


def test_aggregate_is_mean_quantized():
    review = _review(
        {"naming": 0.8, "comments": 0.6, "formatting": 0.9, "structure": 0.7,
         "safety_hygiene": 1.0}
    )
    score, by_dim = _aggregate(review, 0.05)
    assert by_dim["naming"] == 0.8
    assert score == _quantize(0.8, 0.05)  # mean = 0.8


def test_completed_with_mock_gateway(sandbox_executor, tmp_path):
    review = _review(
        {"naming": 0.8, "comments": 0.6, "formatting": 0.9, "structure": 0.7,
         "safety_hygiene": 1.0}
    )
    ctx = make_analyzer_ctx(
        sandbox_executor, tmp_path, "readability", "correct_add.c",
        gateway=MockGateway(review),
    )
    result = readability.analyze(ctx)
    assert result.status == "completed"
    assert result.score == 0.8
    assert result.measurements["dimensions"]["comments"] == 0.6
    assert any("suggestion: rename x" in f for f in result.findings)
    assert any("advisory" in lim for lim in result.limitations)


def test_no_gateway_is_inconclusive(sandbox_executor, tmp_path):
    ctx = make_analyzer_ctx(
        sandbox_executor, tmp_path, "readability", "correct_add.c", gateway=None
    )
    result = readability.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None


def test_gateway_failure_is_inconclusive(sandbox_executor, tmp_path):
    ctx = make_analyzer_ctx(
        sandbox_executor, tmp_path, "readability", "correct_add.c",
        gateway=MockGateway(exc=RuntimeError("connection refused")),
    )
    result = readability.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None
    assert "connection refused" in result.diagnostics["detail"]


def test_empty_review_is_inconclusive_not_zero(sandbox_executor, tmp_path):
    # D14: an empty review used to validate and score a `completed` 0.0 —
    # a manufactured zero from no evidence. It must be inconclusive.
    empty = CodeReview(criteria=[], suggestions=[], confidence=0.0, overall_note="")
    ctx = make_analyzer_ctx(
        sandbox_executor, tmp_path, "readability", "correct_add.c",
        gateway=MockGateway(empty),
    )
    result = readability.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None
    assert "empty" in result.diagnostics["reason"]


def test_partial_review_is_inconclusive_not_averaged(sandbox_executor, tmp_path):
    # D14: a review missing rubric dimensions must not be silently
    # averaged over the dimensions that arrived.
    partial = _review({"naming": 0.9, "comments": 0.9, "formatting": 0.9})
    ctx = make_analyzer_ctx(
        sandbox_executor, tmp_path, "readability", "correct_add.c",
        gateway=MockGateway(partial),
    )
    result = readability.analyze(ctx)
    assert result.status == "inconclusive"
    assert result.score is None
    assert "structure" in result.diagnostics["reason"]
    assert "safety_hygiene" in result.diagnostics["reason"]
