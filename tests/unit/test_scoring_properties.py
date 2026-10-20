"""Property-style tests for the scoring engine (policy 1.1).

Parameterized sweeps over weight distributions, missing-evidence policy
combinations, and rounding edges — plus the policy-1.1 invariants:

- zero eligible evidence -> grade None + ``incomplete`` (D6), never a
  manufactured 0.0;
- a *confirmed* zero (eligible metrics scoring 0) still yields 0.0, and
  the explicit ``zero`` missing-evidence policy still yields 0.0;
- NaN/inf scores are ineligible and never reach the grade;
- every MetricScore exposes configured AND effective weights (D7);
- scoring is a pure function: same inputs -> identical result.
"""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from autograder.app.core.schemas import AnalyzerResult
from autograder.app.scoring import score

from .conftest import make_result, make_rules

_ANALYZERS = ["correctness", "compiler", "static_analysis", "modularity",
              "complexity", "memory_safety", "readability", "similarity"]


def _rules_for(weights, required=None, **overrides):
    metrics = []
    for i, w in enumerate(weights):
        req = True if required is None else required[i]
        metrics.append((f"m{i}", _ANALYZERS[i % len(_ANALYZERS)], w, req))
    return make_rules(metrics, **overrides)


def _results_for(scores, weights):
    return [
        make_result(f"m{i}", _ANALYZERS[i % len(_ANALYZERS)], s)
        for i, s in enumerate(scores)
    ]


# ---------------------------------------------------------------------------
# Weight distributions: grade == 100 * sum(w_i * s_i), half-up to 0.1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "weights,scores,expected",
    [
        ([0.5, 0.3, 0.2], [1.0, 0.5, 0.25], "70.0"),
        ([0.25, 0.25, 0.25, 0.25], [0.2, 0.4, 0.6, 0.8], "50.0"),
        ([0.7, 0.2, 0.1], [0.9, 0.3, 0.0], "69.0"),
        ([1.0], [0.333], "33.3"),
        ([0.1] * 10, [0.5] * 10, "50.0"),
        ([0.6, 0.4], [0.0, 0.0], "0.0"),          # confirmed zero
        ([0.05, 0.95], [1.0, 1.0], "100.0"),      # perfect score
        ([0.34, 0.33, 0.33], [0.5, 0.5, 0.5], "50.0"),
    ],
)
def test_weight_distributions(weights, scores, expected):
    rules = _rules_for(weights)
    out = score(_results_for(scores, weights), rules)
    assert out.grade == Decimal(expected)
    total = sum(m.contribution for m in out.per_metric)
    assert abs(total - out.grade) <= Decimal("0.05")
    for m in out.per_metric:
        # No renormalization happened: configured == effective == weight.
        assert m.configured_weight == m.effective_weight == m.weight


def test_purity_same_inputs_same_result():
    rules = _rules_for([0.5, 0.3, 0.2])
    results = _results_for([0.9, 0.4, 0.7], [0.5, 0.3, 0.2])
    assert score(results, rules) == score(results, rules)


# ---------------------------------------------------------------------------
# Rounding edges (half-up, never banker's)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "weights,scores,expected",
    [
        ([1.0], [0.125], "12.5"),
        ([1.0], [0.124], "12.4"),
        ([1.0], [0.115], "11.5"),   # 11.45 -> would be 11.4 banker's on floats
        ([0.5, 0.5], [0.649, 0.0], "32.5"),  # exactly 32.45 -> half-up 32.5
        ([0.5, 0.5], [0.641, 0.0], "32.1"),  # 32.05 -> 32.1 half-up
        ([1.0], [0.005], "0.5"),
        ([1.0], [0.004], "0.4"),
    ],
)
def test_rounding_half_up_edges(weights, scores, expected):
    rules = _rules_for(weights)
    out = score(_results_for(scores, weights), rules)
    assert out.grade == Decimal(expected)


# ---------------------------------------------------------------------------
# Missing-evidence matrix: policy x required/optional x eligibility
# ---------------------------------------------------------------------------

def _policy(req_missing, opt_missing):
    return {"uncertainty_policy": {
        "required_metric_missing": req_missing,
        "optional_metric_missing": opt_missing}}


@pytest.mark.parametrize("opt_policy", ["renormalize", "exclude"])
def test_no_eligible_evidence_is_none_not_zero(opt_policy):
    """D6: all metrics optional + unavailable -> None/incomplete (1.1).

    (Under policy 1.0 this manufactured grade 0.0.)"""
    rules = _rules_for([0.6, 0.4], required=[False, False],
                       **_policy("incomplete", opt_policy))
    out = score([], rules)
    assert out.grade is None
    assert out.status == "incomplete"
    assert any("no eligible metric evidence" in w for w in out.warnings)


def test_no_eligible_evidence_zero_policy_is_explicit_zero():
    """The `zero` policy is an explicit instructor choice: 0.0 stands."""
    rules = _rules_for([0.6, 0.4], required=[False, False],
                       **_policy("incomplete", "zero"))
    out = score([], rules)
    assert out.grade == Decimal("0.0")
    assert out.status == "completed_with_warnings"
    for m in out.per_metric:
        assert m.effective_weight == m.configured_weight  # weight kept


def test_required_provisional_all_missing_is_none():
    """Provisional renormalizes over *remaining* metrics; with none
    remaining there is nothing to renormalize over -> None (D6)."""
    rules = _rules_for([0.6, 0.4], required=[True, True],
                       **_policy("provisional", "renormalize"))
    out = score([], rules)
    assert out.grade is None
    assert out.status == "incomplete"
    assert any("no eligible metric evidence" in w for w in out.warnings)


def test_empty_results_all_required_is_incomplete():
    rules = _rules_for([1.0], required=[True])
    out = score([], rules)
    assert out.grade is None and out.status == "incomplete"


@pytest.mark.parametrize(
    "opt_policy,expected",
    [
        ("renormalize", "80.0"),   # 0.5*0.8 / 0.5 -> rescaled to full weight
        ("exclude", "40.0"),       # 100 * 0.5*0.8, weights untouched
        ("zero", "40.0"),          # missing contributes 0 with weight kept
    ],
)
def test_optional_missing_policies_with_eligible(opt_policy, expected):
    rules = _rules_for([0.5, 0.5], required=[True, False],
                       **_policy("incomplete", opt_policy))
    out = score([make_result("m0", "correctness", 0.8)], rules)
    assert out.grade == Decimal(expected)
    assert out.status == "completed_with_warnings"


def test_confirmed_zero_distinguishable_from_no_evidence():
    """Eligible metrics scoring 0 -> grade 0.0 + complete (not incomplete)."""
    rules = _rules_for([0.6, 0.4])
    out = score(_results_for([0.0, 0.0], [0.6, 0.4]), rules)
    assert out.grade == Decimal("0.0")
    assert out.status == "complete"
    assert not any("no eligible metric evidence" in w for w in out.warnings)


# ---------------------------------------------------------------------------
# Dual weights (D7)
# ---------------------------------------------------------------------------

def test_dual_weights_under_renormalization():
    rules = _rules_for([0.5, 0.3, 0.2], required=[True, False, True],
                       **_policy("incomplete", "renormalize"))
    out = score([make_result("m0", "correctness", 0.8),
                 make_result("m2", "static_analysis", 0.6)], rules)
    by_name = {m.name: m for m in out.per_metric}
    tol = Decimal("1e-20")
    # Eligible metrics: configured preserved, effective rescaled by 1/0.7.
    assert by_name["m0"].configured_weight == Decimal("0.5")
    assert abs(by_name["m0"].effective_weight
               - Decimal("0.5") / Decimal("0.7")) < tol
    assert by_name["m0"].weight == by_name["m0"].effective_weight
    assert by_name["m2"].configured_weight == Decimal("0.2")
    assert abs(by_name["m2"].effective_weight
               - Decimal("0.2") / Decimal("0.7")) < tol
    # Excluded metric: configured preserved, effective 0.
    assert by_name["m1"].configured_weight == Decimal("0.3")
    assert by_name["m1"].effective_weight == Decimal("0")
    # Contributions reconcile against EFFECTIVE weights.
    for m in out.per_metric:
        if m.score is not None and m.status == "completed":
            assert m.contribution == 100 * m.effective_weight * m.score


def test_dual_weights_provisional_required_excluded():
    rules = _rules_for([0.6, 0.4], required=[True, True],
                       **_policy("provisional", "renormalize"))
    out = score([make_result("m1", "compiler", 0.75)], rules)
    by_name = {m.name: m for m in out.per_metric}
    assert by_name["m0"].configured_weight == Decimal("0.6")
    assert by_name["m0"].effective_weight == Decimal("0")
    assert by_name["m1"].effective_weight == Decimal("1")  # renormalized
    assert out.grade == Decimal("75.0")


# ---------------------------------------------------------------------------
# Non-finite scores never reach the grade
# ---------------------------------------------------------------------------

def _raw_result(name, analyzer, score_value, status="completed"):
    """Build an AnalyzerResult bypassing schema validation.

    The AnalyzerResult schema itself rejects NaN/inf; the engine must
    still be robust if a result arrives via model_construct or a future
    schema change — defense in depth, exercised here directly.
    """
    return AnalyzerResult.model_construct(
        metric_name=name,
        analyzer_id=analyzer,
        analyzer_version="1.0",
        status=status,
        measurements={},
        score=score_value,
        evidence_refs=[],
        findings=[],
        warnings=[],
        limitations=[],
        diagnostics={},
        execution_metadata={},
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_score_required_metric(bad):
    rules = _rules_for([1.0], required=[True])
    out = score([_raw_result("m0", "correctness", bad)], rules)
    assert out.grade is None
    assert out.status == "incomplete"
    assert any("not finite" in w for w in out.warnings)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_nonfinite_score_optional_metric_excluded(bad):
    rules = _rules_for([0.5, 0.5], required=[True, False],
                       **_policy("incomplete", "renormalize"))
    out = score([make_result("m0", "correctness", 0.8),
                 _raw_result("m1", "compiler", bad)], rules)
    # Only the finite metric counts (renormalized to full weight).
    assert out.grade == Decimal("80.0")
    assert math.isfinite(float(out.grade))
    assert any("not finite" in w for w in out.warnings)


def test_out_of_range_score_ineligible():
    rules = _rules_for([1.0], required=[True])
    out = score([_raw_result("m0", "correctness", 1.5)], rules)
    assert out.grade is None and out.status == "incomplete"
