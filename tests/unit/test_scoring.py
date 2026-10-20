"""Unit tests: deterministic scoring engine."""

from __future__ import annotations

from decimal import Decimal

import pytest

from autograder.app.scoring import POLICY_VERSION, score

from .conftest import make_result, make_rules


def test_weighted_grade_math():
    rules = make_rules([("correctness", "correctness", 0.6), ("compiler", "compiler", 0.4)])
    results = [
        make_result("correctness", "correctness", 0.8),
        make_result("compiler", "compiler", 0.5),
    ]
    out = score(results, rules)
    # 100 * (0.6*0.8 + 0.4*0.5) = 68.0
    assert out.grade == Decimal("68.0")
    assert out.status == "complete"
    assert out.policy_version == POLICY_VERSION
    contribs = {m.name: m.contribution for m in out.per_metric}
    assert contribs["correctness"] == Decimal("48.0")
    assert contribs["compiler"] == Decimal("20.0")


def test_rounding_half_up():
    rules = make_rules([("a", "correctness", 0.5), ("b", "compiler", 0.5)])
    # 100*(0.5*0.633 + 0.5*0.700) = 66.65 -> half-up = 66.7
    # (banker's rounding would give 66.6 — this test pins half-up)
    out = score([make_result("a", "correctness", 0.633),
                 make_result("b", "compiler", 0.700)], rules)
    assert out.grade == Decimal("66.7")


def test_rounding_down():
    rules = make_rules([("a", "correctness", 0.5), ("b", "compiler", 0.5)])
    # 100*(0.5*0.6328 + 0.5*0.700) = 66.64 -> 66.6
    out = score([make_result("a", "correctness", 0.6328),
                 make_result("b", "compiler", 0.700)], rules)
    assert out.grade == Decimal("66.6")


def test_required_metric_missing_means_incomplete():
    rules = make_rules([("correctness", "correctness", 0.6), ("compiler", "compiler", 0.4)])
    out = score([make_result("compiler", "compiler", 0.9)], rules)
    assert out.grade is None
    assert out.status == "incomplete"
    assert any("correctness" in w for w in out.warnings)


def test_required_metric_missing_provisional():
    rules = make_rules(
        [("correctness", "correctness", 0.6), ("compiler", "compiler", 0.4)],
        uncertainty_policy={"required_metric_missing": "provisional",
                            "optional_metric_missing": "renormalize"},
    )
    out = score([make_result("compiler", "compiler", 0.75)], rules)
    # provisional: remaining weight renormalized to 1.0 -> 75.0
    assert out.grade == Decimal("75.0")
    assert out.status == "completed_with_warnings"
    assert any("provisional" in w for w in out.warnings)


def test_optional_missing_renormalize():
    rules = make_rules(
        [("a", "correctness", 0.5), ("b", "compiler", 0.3, False), ("c", "similarity", 0.2)],
        uncertainty_policy={"required_metric_missing": "incomplete",
                            "optional_metric_missing": "renormalize"},
    )
    out = score([make_result("a", "correctness", 0.8),
                 make_result("c", "similarity", 0.6)], rules)
    # (0.5*0.8 + 0.2*0.6) / 0.7 = 0.52/0.7 = 74.2857 -> 74.3
    assert out.status == "completed_with_warnings"
    assert abs(float(out.grade) - 74.3) < 0.01
    assert any("renormaliz" in w for w in out.warnings)


def test_optional_missing_zero():
    rules = make_rules(
        [("a", "correctness", 0.5), ("b", "compiler", 0.3, False), ("c", "similarity", 0.2)],
        uncertainty_policy={"required_metric_missing": "incomplete",
                            "optional_metric_missing": "zero"},
    )
    out = score([make_result("a", "correctness", 0.8),
                 make_result("c", "similarity", 0.6)], rules)
    # 100*(0.5*0.8 + 0.3*0 + 0.2*0.6) = 52.0, weight kept
    assert out.grade == Decimal("52.0")
    b = next(m for m in out.per_metric if m.name == "b")
    assert b.score == Decimal("0") and b.contribution == Decimal("0")


def test_optional_missing_exclude():
    rules = make_rules(
        [("a", "correctness", 0.5), ("b", "compiler", 0.3, False), ("c", "similarity", 0.2)],
        uncertainty_policy={"required_metric_missing": "incomplete",
                            "optional_metric_missing": "exclude"},
    )
    out = score([make_result("a", "correctness", 0.8),
                 make_result("c", "similarity", 0.6)], rules)
    assert out.grade == Decimal("52.0")
    b = next(m for m in out.per_metric if m.name == "b")
    assert b.score is None and "excluded" in b.note


def test_failed_analyzer_treated_as_missing():
    rules = make_rules([("correctness", "correctness", 0.6), ("compiler", "compiler", 0.4)])
    out = score([make_result("correctness", "correctness", None, status="failed"),
                 make_result("compiler", "compiler", 0.5)], rules)
    assert out.grade is None and out.status == "incomplete"


def test_inconclusive_never_contributes():
    # An inconclusive result WITH a score must not move the grade.
    rules = make_rules([("correctness", "correctness", 1.0)])
    out = score([make_result("correctness", "correctness", 0.9, status="inconclusive")],
                rules)
    assert out.grade is None and out.status == "incomplete"


def test_skipped_optional_excluded_no_evidence_is_incomplete():
    # UPDATED for scoring policy 1.1 (D6): this test previously pinned the
    # defective behavior "grade over empty set -> 0.0 with warnings". A
    # grade computed from ZERO eligible metrics is manufactured evidence:
    # with no eligible metric and no explicit `zero` policy in play, the
    # engine must now return grade None + status incomplete + the explicit
    # reason "no eligible metric evidence" — even when the only metric is
    # optional. (An eligible metric scoring 0, or the explicit `zero`
    # policy, still yields a defensible 0.0 — see the property tests.)
    rules = make_rules(
        [("correctness", "correctness", 1.0, False)],
        uncertainty_policy={"required_metric_missing": "incomplete",
                            "optional_metric_missing": "exclude"},
    )
    out = score([make_result("correctness", "correctness", None, status="skipped")],
                rules)
    assert out.grade is None
    assert out.status == "incomplete"
    assert any("no eligible metric evidence" in w for w in out.warnings)


def test_all_required_missing_gives_incomplete():
    rules = make_rules([("correctness", "correctness", 1.0)])
    out = score([], rules)
    assert out.grade is None and out.status == "incomplete"


def test_duplicate_results_rejected():
    rules = make_rules([("correctness", "correctness", 1.0)])
    with pytest.raises(ValueError, match="duplicate result"):
        score([make_result("correctness", "correctness", 0.5),
               make_result("correctness", "correctness", 0.6)], rules)


def test_unknown_metric_result_warns_and_ignored():
    rules = make_rules([("correctness", "correctness", 1.0)])
    out = score([make_result("correctness", "correctness", 0.5),
                 make_result("mystery", "correctness", 1.0)], rules)
    assert out.grade == Decimal("50.0")
    assert any("mystery" in w for w in out.warnings)


def test_per_metric_covers_all_rule_metrics():
    rules = make_rules([("correctness", "correctness", 0.6), ("compiler", "compiler", 0.4)])
    out = score([make_result("compiler", "compiler", 0.5)], rules)
    assert {m.name for m in out.per_metric} == {"correctness", "compiler"}


def test_contributions_sum_to_grade():
    rules = make_rules(
        [("a", "correctness", 0.3), ("b", "compiler", 0.3), ("c", "similarity", 0.4)]
    )
    out = score([make_result("a", "correctness", 0.91),
                 make_result("b", "compiler", 0.77),
                 make_result("c", "similarity", 0.83)], rules)
    total = sum(m.contribution for m in out.per_metric)
    # unrounded contributions reconcile with the rounded grade within 0.05
    assert abs(total - out.grade) <= Decimal("0.05")
