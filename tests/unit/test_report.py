"""Unit tests: report schema validation and totals reconciliation."""

from __future__ import annotations

import pytest

from autograder.app.core.schemas import (
    AssignmentRef,
    ExpectedProvenance,
    FailingTest,
    ReproductionInfo,
    RulesRef,
    SubmissionRef,
)
from autograder.app.reporting import ReportError, validate_report
from autograder.app.reporting.schemas import Report
from autograder.app.scoring import POLICY_VERSION, score

from .conftest import make_result, make_rules


def build_report_dict(grade_override=None, contrib_override=None):
    """Build a fully consistent report via the real scoring engine."""
    rules = make_rules([("correctness", "correctness", 0.6), ("compiler", "compiler", 0.4)])
    out = score([make_result("correctness", "correctness", 0.8),
                 make_result("compiler", "compiler", 0.5)], rules)
    metrics = []
    for m in out.per_metric:
        contrib = float(m.contribution)
        if contrib_override is not None:
            contrib = contrib_override
        metrics.append({
            "name": m.name, "analyzer": m.analyzer, "weight": float(m.weight),
            "score": float(m.score), "contribution": contrib, "status": m.status,
        })
    grade = float(out.grade) if out.grade is not None else None
    if grade_override is not None:
        grade = grade_override
    return {
        "run_id": "r1",
        "submission": {"id": "s1", "source_hash": "sha256:aa"},
        "assignment": {"id": "a1", "spec_hash": "sha256:bb"},
        "rules": {"version": "1.0.0", "hash": "sha256:cc"},
        "grade": grade,
        "status": "completed",
        "metrics": metrics,
        "analyzer_statuses": {"correctness": "completed", "compiler": "completed"},
        "scoring_policy_version": POLICY_VERSION,
        "failing_tests": [{
            "test_id": "t1", "input": "2 3\n", "expected": "5\n",
            "expected_provenance": {"tier": "tier2_independent",
                                    "method": "spec example", "detail": "p1"},
            "actual": "6\n", "note": "off by one",
        }],
        "reproduction": {"run_id": "r1", "rules_hash": "sha256:cc",
                         "scoring_policy_version": POLICY_VERSION},
    }


def test_valid_report_passes():
    r = validate_report(build_report_dict())
    assert r.grade == 68.0
    assert len(r.failing_tests) == 1


def test_totals_must_reconcile():
    with pytest.raises(ReportError, match="reconcile"):
        validate_report(build_report_dict(grade_override=90.0))


def test_contribution_must_match_weight_times_score():
    with pytest.raises(ReportError, match="contribution"):
        validate_report(build_report_dict(contrib_override=1.0))


def test_policy_version_must_match_engine():
    d = build_report_dict()
    d["scoring_policy_version"] = "0.1"
    with pytest.raises(ReportError, match="scoring_policy_version"):
        validate_report(d)


def test_incomplete_report_grade_none_ok():
    d = build_report_dict()
    d["grade"] = None
    d["status"] = "incomplete"
    r = validate_report(d)
    assert r.grade is None


def test_grade_out_of_range_rejected():
    d = build_report_dict(grade_override=101.0)
    with pytest.raises(ReportError):
        validate_report(d)


def test_missing_required_section_rejected():
    d = build_report_dict()
    del d["submission"]
    with pytest.raises(ReportError):
        validate_report(d)


def test_accepts_report_instance():
    r = validate_report(build_report_dict())
    assert validate_report(r).run_id == "r1"
