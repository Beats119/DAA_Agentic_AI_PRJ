"""Unit tests: TestCase / oracle provenance / Evidence / AnalyzerResult schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from autograder.app.core.schemas import (
    AnalyzerResult,
    Evidence,
    ExpectedOutput,
    ExpectedProvenance,
)
from autograder.app.core.schemas import TestCase as TestCaseModel

TestCaseModel.__test__ = False  # a Pydantic model, not a pytest test class


def prov(tier="tier2_independent"):
    return ExpectedProvenance(tier=tier, method="spec example", detail="p2")


def case(**kw):
    base = dict(
        id="t1",
        input="2 3\n",
        input_category="normal",
        rationale="basic addition",
        requirement_refs=["r1"],
        expected=ExpectedOutput(value="5\n", provenance=prov()),
    )
    base.update(kw)
    return TestCaseModel(**base)


@pytest.mark.parametrize("status", ["trusted", "provisional", "rejected", "inconclusive"])
def test_oracle_statuses_accepted(status):
    assert case(status=status).status == status


def test_bad_oracle_status_rejected():
    with pytest.raises(ValidationError):
        case(status="maybe")


def test_bad_tier_rejected():
    with pytest.raises(ValidationError):
        ExpectedProvenance(tier="vibes", method="m", detail="d")


def test_testcase_immutable():
    tc = case()
    with pytest.raises(ValidationError):
        tc.status = "trusted"  # type: ignore[misc]


def test_evidence_id_pattern():
    ev = Evidence(id="ev:abc123:7", analyzer="correctness", run_id="abc123",
                  kind="test_result")
    assert ev.id == "ev:abc123:7"
    with pytest.raises(ValidationError):
        Evidence(id="not-an-evidence-id", analyzer="x", run_id="r", kind="k")


def test_evidence_immutable():
    ev = Evidence(id="ev:r:1", analyzer="x", run_id="r", kind="k")
    with pytest.raises(ValidationError):
        ev.severity = "high"  # type: ignore[misc]


def test_evidence_confidence_bounds():
    with pytest.raises(ValidationError):
        Evidence(id="ev:r:1", analyzer="x", run_id="r", kind="k", confidence=1.5)


def test_analyzer_result_score_range():
    ok = AnalyzerResult(metric_name="m", analyzer_id="a", analyzer_version="1",
                        status="completed", score=0.5)
    assert ok.score == 0.5
    for bad in (-0.1, 1.1, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            AnalyzerResult(metric_name="m", analyzer_id="a", analyzer_version="1",
                           status="completed", score=bad)


def test_analyzer_result_score_optional():
    r = AnalyzerResult(metric_name="m", analyzer_id="a", analyzer_version="1",
                       status="skipped")
    assert r.score is None


@pytest.mark.parametrize("status", ["completed", "failed", "skipped", "unsupported", "inconclusive"])
def test_analyzer_statuses(status):
    r = AnalyzerResult(metric_name="m", analyzer_id="a", analyzer_version="1",
                       status=status)
    assert r.status == status


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        TestCaseModel(**dict(case().model_dump(), bogus=1))
