"""Unit tests: deterministic feedback verification — D21 checks.

Covers the two verification gaps the audit recorded as D21:

- **Source-location claims**: cited lines (``line 42``, ``lines 10-15``,
  ``add.c:33``, ``:33``) must exist in the submission. The check runs
  only when the caller supplies the source line count; the workflow
  always does.
- **Metric-score claims**: a claimed metric score must name a metric in
  the run's metric set and match its recorded score (0-1 or 0-100
  scale). Runs only when the caller supplies per-metric scores.

Violations feed the existing bounded-regeneration / fallback path in
``finalize_feedback`` — proven end-to-end by the fallback test below.
"""

from __future__ import annotations

from autograder.app.core.schemas import GenerationRecord
from autograder.app.orchestration.evidence import EvidenceRegistry
from autograder.app.orchestration.feedback import (
    FeedbackFailure,
    FeedbackText,
    finalize_feedback,
    verify_feedback,
)


def _registry() -> tuple[EvidenceRegistry, str]:
    registry = EvidenceRegistry("fbverify")
    ev_id = registry.append(
        "test_result", "correctness",
        {"test_id": "t1", "passed": False, "input": "2 3",
         "expected": "5", "actual": "6"},
    )
    registry.finalize()
    return registry, ev_id


_TRUSTED = {"t1": {"passed": False, "input": "2 3", "expected": "5",
                   "actual": "6"}}


def _verify(feedback: FeedbackText, **kwargs):
    registry, _ = _registry()
    return verify_feedback(feedback, registry, dict(_TRUSTED), None, None,
                           **kwargs)


# --------------------------------------------------------------------- #
# Source-location claims
# --------------------------------------------------------------------- #

def test_line_citation_out_of_range_rejected():
    fb = FeedbackText(summary="The bug is on line 9999 of the submission.")
    result = _verify(fb, source_lines=20)
    assert not result.ok
    assert any("cites line 9999" in v and "20 lines" in v
               for v in result.violations), result.violations


def test_line_citation_within_range_passes():
    _, ev_id = _registry()
    fb = FeedbackText(
        summary="See line 5 for the input loop.",
        per_failure=[FeedbackFailure(
            evidence_id=ev_id,
            explanation="The addition on line 7 overflows.",
            suggestion="Check the guard near add.c:12 before summing.",
        )],
        recommendations=["Re-read lines 3-9 and trace the values."],
    )
    result = _verify(fb, source_lines=20)
    assert result.ok, result.violations


def test_line_range_endpoints_are_checked():
    fb = FeedbackText(summary="The loop spans lines 18-25.")
    result = _verify(fb, source_lines=20)
    assert not result.ok
    assert any("cites line 25" in v for v in result.violations)


def test_file_line_citation_out_of_range_rejected():
    fb = FeedbackText(summary="The defect sits at add.c:33.")
    result = _verify(fb, source_lines=20)
    assert not result.ok
    assert any("cites line 33" in v for v in result.violations)


def test_bare_colon_citation_checked():
    fb = FeedbackText(summary="Look again at :33 in the source.")
    result = _verify(fb, source_lines=20)
    assert not result.ok
    assert any("cites line 33" in v for v in result.violations)


def test_non_citations_do_not_trigger():
    # Times, ratios and key:value text are not line citations.
    fb = FeedbackText(
        summary="It ran at 12:30 with a 1:3 split; expected:5, actual:6.")
    result = _verify(fb, source_lines=20)
    assert result.ok, result.violations


def test_line_zero_rejected():
    fb = FeedbackText(summary="Start from line 0.")
    result = _verify(fb, source_lines=20)
    assert not result.ok
    assert any("cites line 0" in v for v in result.violations)


def test_location_check_skipped_without_source_lines():
    # No line count supplied -> the check is skipped, never guessed.
    fb = FeedbackText(summary="The bug is on line 9999.")
    result = _verify(fb)
    assert result.ok, result.violations


class _LineCitingGateway:
    """TEST-ONLY gateway whose drafts always cite a nonexistent line."""

    def generate_structured(self, *, task, prompt, schema, model_cfg,
                            prompt_name=None, **kw):
        record = GenerationRecord(
            task=task, model="mock-model", temperature=0.0, seed=42,
            timeout_s=120.0, prompt_version="1.0",
            prompt_hash="sha256:" + "0" * 16, duration_s=0.01)
        return FeedbackText(
            summary="The defect is on line 9999."), record


def test_out_of_range_citation_falls_back():
    registry, _ = _registry()
    out = finalize_feedback(
        evidence_json=registry.to_json(),
        gateway=_LineCitingGateway(),
        registry=registry,
        trusted_results=dict(_TRUSTED),
        grade=None,
        failing_tests=[],
        analyzer_notes=[],
        uncertainty_notes=[],
        test_stats={"generated": 0, "trusted": 0},
        run_status="incomplete",
        max_retries=1,
        source_lines=20,
    )
    assert out["used_fallback"] is True
    assert out["attempts"] == 2  # bounded: 1 initial + 1 retry
    assert any("cites line 9999" in v for v in out["violations"])


# --------------------------------------------------------------------- #
# Metric-score claims
# --------------------------------------------------------------------- #

_SCORES = {"correctness": 0.8, "compiler": 1.0, "modularity": None}


def test_metric_score_claim_correct_fraction():
    fb = FeedbackText(summary="Correctness scored 0.8 on trusted tests.")
    result = _verify(fb, metric_scores=_SCORES)
    assert result.ok, result.violations


def test_metric_score_claim_correct_percent():
    fb = FeedbackText(summary="Correctness scored 80 overall.")
    result = _verify(fb, metric_scores=_SCORES)
    assert result.ok, result.violations


def test_metric_score_claim_score_for_form():
    fb = FeedbackText(summary="The score for correctness is 0.8.")
    result = _verify(fb, metric_scores=_SCORES)
    assert result.ok, result.violations


def test_metric_score_claim_wrong_value_rejected():
    fb = FeedbackText(summary="Correctness scored 0.5 on trusted tests.")
    result = _verify(fb, metric_scores=_SCORES)
    assert not result.ok
    assert any("correctness" in v and "0.8" in v
               for v in result.violations), result.violations


def test_metric_score_claim_unknown_metric_rejected():
    fb = FeedbackText(summary="The metric velocity scored 0.5.")
    result = _verify(fb, metric_scores=_SCORES)
    assert not result.ok
    assert any("unknown metric" in v and "velocity" in v
               for v in result.violations), result.violations


def test_metric_score_claim_unknown_snake_case_rejected():
    fb = FeedbackText(summary="The score for memory_safety was 0.9.")
    result = _verify(fb, metric_scores=_SCORES)
    assert not result.ok
    assert any("unknown metric" in v and "memory_safety" in v
               for v in result.violations), result.violations


def test_metric_score_claim_unscored_metric_rejected():
    fb = FeedbackText(summary="Modularity scored 0.5.")
    result = _verify(fb, metric_scores=_SCORES)
    assert not result.ok
    assert any("modularity" in v and "no recorded score" in v
               for v in result.violations), result.violations


def test_metric_claims_skipped_without_metric_scores():
    fb = FeedbackText(summary="Correctness scored 0.5 on trusted tests.")
    result = _verify(fb)
    assert result.ok, result.violations


def test_plain_prose_is_not_a_metric_claim():
    fb = FeedbackText(
        summary="The program scored 5 on the sample input, "
                "and the score of the submission reflects that.")
    result = _verify(fb, metric_scores=_SCORES)
    assert result.ok, result.violations
