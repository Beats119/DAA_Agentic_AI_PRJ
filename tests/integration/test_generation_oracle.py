"""Generation + validation + oracle tests — mock LLM, synthetic fixtures.

Covers the full candidate lifecycle: LLM proposes -> deterministic
validation -> tiered oracle assessment. Asserts the critical invariant:
Tier-3 agreement NEVER yields ``trusted``.
"""

from __future__ import annotations

import json


from conftest import make_rules

from autograder.app.core.schemas import (
    Specification,
    Requirement,
    RequirementSource,
    SpecExample,
    TestCandidate as CandidateModel,
)
from autograder.app.ollama.client import OllamaClient
from autograder.app.testing.generation.agent import (
    candidate_to_testcase,
    generate_test_suite,
)
from autograder.app.testing.oracle.service import OracleService
from autograder.app.testing.oracle.strategies import OracleContext
from autograder.app.testing.validation.cases import validate_candidate


def _spec():
    return Specification(
        requirements=[
            Requirement(
                id="R1",
                text="Read two integers from stdin.",
                kind="explicit",
                source=RequirementSource(page=1, excerpt="read two integers"),
            ),
            Requirement(
                id="R2",
                text="Print their sum.",
                kind="explicit",
                source=RequirementSource(page=1, excerpt="print their sum"),
            ),
        ],
        examples=[SpecExample(input="2 3\n", output="5\n", page=1)],
    )


def _rules(**over):
    return make_rules(
        [("correctness", "correctness", 1.0)],
        test_generation={"max_rounds": 2, "max_candidates": 8,
                         "token_budget": 5000, "exec_budget": 30},
        **over,
    )


CANDIDATES = {
    "candidates": [
        {"id": "T1", "input": "2 3\n", "input_category": "normal",
         "rationale": "spec example", "requirement_refs": ["R1", "R2"],
         "expected_value": "5\n"},
        {"id": "T2", "input": "-1 1\n", "input_category": "edge",
         "rationale": "negatives", "requirement_refs": ["R1"],
         "expected_value": "0\n"},
        {"id": "T3", "input": "2 3\n", "input_category": "normal",
         "rationale": "duplicate input", "requirement_refs": ["R1"],
         "expected_value": "5\n"},
        {"id": "T4", "input": "", "input_category": "edge",
         "rationale": "empty", "requirement_refs": ["R9"],
         "expected_value": ""},
    ]
}


def test_validation_rejects_bad_candidates():
    spec = _spec()
    ok = CandidateModel(id="T1", input="1 2\n", input_category="normal",
                       rationale="r", requirement_refs=["R1"], expected_value="3\n")
    assert validate_candidate(ok, spec).ok

    bad_empty = ok.model_copy(update={"input": ""})
    assert not validate_candidate(bad_empty, spec).ok

    bad_ref = ok.model_copy(update={"requirement_refs": ["R99"]})
    res = validate_candidate(bad_ref, spec)
    assert not res.ok and any("R99" in e for e in res.errors)

    bad_cat = ok.model_copy(update={"input_category": "weird"})
    assert not validate_candidate(bad_cat, spec).ok


def test_oracle_spec_example_trusted_and_rejected():
    spec = _spec()
    rules = _rules()
    svc = OracleService()
    ctx = OracleContext()

    match = candidate_to_testcase(CandidateModel(
        id="T1", input="2 3\n", input_category="normal", rationale="r",
        requirement_refs=["R1"], expected_value="5\n"))
    updated, _ = svc.assess(match, spec, rules, ctx)
    assert updated.status == "trusted"
    assert updated.expected.provenance.tier == "tier2_independent"

    clash = candidate_to_testcase(CandidateModel(
        id="T2", input="2 3\n", input_category="normal", rationale="r",
        requirement_refs=["R1"], expected_value="6\n"))
    updated2, _ = svc.assess(clash, spec, rules, ctx)
    assert updated2.status == "rejected"


def test_oracle_tier3_agreement_never_trusted(mock_ollama):
    """The critical invariant: LLM agreement -> provisional at best."""
    url, server = mock_ollama
    # Second path agrees with the candidate.
    server.script["generate"] = [
        ("json", {"response": json.dumps(
            {"reasoning": "2+3=5", "expected_output": "5\n"})})
    ]
    spec = Specification(requirements=[], examples=[])  # no spec anchors
    # Tier-3 is opt-in (default off); this test exercises the tier
    # itself, so the instructor opts in explicitly.
    rules = _rules(oracle={"tier3_llm_verification": True})
    svc = OracleService()
    client = OllamaClient(url)
    try:
        ctx = OracleContext(gateway=client)
        cand = candidate_to_testcase(CandidateModel(
            id="T9", input="2 3\n", input_category="normal", rationale="r",
            requirement_refs=[], expected_value="5\n"))
        updated, chain = svc.assess(cand, spec, rules, ctx)
    finally:
        client.close()
    assert updated.status == "provisional", f"chain: {chain}"
    assert updated.status != "trusted"
    assert updated.expected.provenance.tier == "tier3_llm"


def test_oracle_tier3_disagreement_inconclusive(mock_ollama):
    url, server = mock_ollama
    server.script["generate"] = [
        ("json", {"response": json.dumps(
            {"reasoning": "hmm", "expected_output": "42\n"})})
    ]
    spec = Specification(requirements=[], examples=[])
    # Opt in: without it the chain is inconclusive via the policy skip,
    # which would make this disagreement assertion vacuous.
    rules = _rules(oracle={"tier3_llm_verification": True})
    svc = OracleService()
    client = OllamaClient(url)
    try:
        ctx = OracleContext(gateway=client)
        cand = candidate_to_testcase(CandidateModel(
            id="T9", input="2 3\n", input_category="normal", rationale="r",
            requirement_refs=[], expected_value="5\n"))
        updated, _ = svc.assess(cand, spec, rules, ctx)
    finally:
        client.close()
    assert updated.status == "inconclusive"


def test_full_suite_budgets_and_quarantine(mock_ollama):
    url, server = mock_ollama
    # Round 1: 4 candidates (T3 dup input, T4 bad ref + empty input).
    # Round 2 (refinement): 1 fresh candidate.
    server.script["generate"] = [
        ("json", {"response": json.dumps(CANDIDATES)}),
        ("json", {"response": json.dumps({"candidates": [
            {"id": "T5", "input": "10 20\n", "input_category": "normal",
             "rationale": "more", "requirement_refs": ["R1"],
             "expected_value": "30\n"}]})}),
    ]
    rules = _rules()
    client = OllamaClient(url)
    try:
        summary = generate_test_suite(_spec(), rules, client)
    finally:
        client.close()
    ids = {tc.id for tc in summary["candidates"]}
    assert ids == {"T1", "T2", "T5"}, ids
    assert summary["rounds_used"] <= 2  # budget respected
    reasons = {q["id"]: q["reason"] for q in summary["quarantined"]}
    assert "T3" in reasons and "duplicate input" in reasons["T3"]
    assert "T4" in reasons  # empty input + unknown R9
    # T1 matches the spec example; T2/T5 are novel.
    assert summary["budgets"]["max_rounds"] == 2


def test_instructor_expected_is_tier1_trusted():
    spec = _spec()
    rules = _rules()
    svc = OracleService()
    ctx = OracleContext(instructor_expected={"7 8\n": "15\n"})
    cand = candidate_to_testcase(CandidateModel(
        id="T7", input="7 8\n", input_category="normal", rationale="r",
        requirement_refs=["R1"], expected_value="15\n"))
    updated, chain = svc.assess(cand, spec, rules, ctx)
    assert updated.status == "trusted"
    assert updated.expected.provenance.tier == "instructor"
    assert chain[0]["strategy"] == "instructor_expected"


def test_reference_oracle_honest_without_executor():
    from autograder.app.testing.oracle.strategies import ReferenceSolutionOracle

    oracle = ReferenceSolutionOracle("int main(){return 0;}")
    oracle.prepare(lambda src: {"ok": True, "binary": "/tmp/ref"}, _spec())
    verdict = oracle.assess(
        candidate_to_testcase(CandidateModel(
            id="T1", input="1\n", input_category="normal", rationale="r",
            requirement_refs=[], expected_value="1\n")),
        _spec(),
        OracleContext(),  # no executor
    )
    assert verdict.status == "inconclusive"
    assert "no execution backend wired" in verdict.provenance.detail


# ---------------------------------------------------------------------------
# D8/D9: reference validation coverage + no trust without comparison
# ---------------------------------------------------------------------------

def _sum_executor(stdin_text):
    try:
        a, b = (int(x) for x in stdin_text.split())
    except Exception:
        return None
    return f"{a + b}\n"


def _tc(id_, input_, expected):
    return candidate_to_testcase(CandidateModel(
        id=id_, input=input_, input_category="normal", rationale="r",
        requirement_refs=["R1"], expected_value=expected))


def test_reference_validated_compare_trusted_and_rejected():
    """D9: with a validated reference, the ctx-executor path COMPARES —
    agreement -> trusted, contradiction -> rejected (with both outputs)."""
    from autograder.app.testing.oracle.strategies import ReferenceSolutionOracle

    oracle = ReferenceSolutionOracle("int main(){/* adds */}")
    prep = oracle.prepare(
        lambda src: {"ok": True, "binary": "/tmp/ref"}, _spec(),
        run_reference=lambda b, s: _sum_executor(s))
    assert prep["ready"] is True
    assert prep["validated"] is True
    assert prep["examples_total"] == 1 and prep["examples_reproduced"] == 1

    ctx = OracleContext(reference_executor=_sum_executor)
    v = oracle.assess(_tc("T1", "4 5\n", "9\n"), _spec(), ctx)
    assert v.status == "trusted"
    assert v.evidence["reference_stdout"] == "9\n"

    v2 = oracle.assess(_tc("T2", "4 5\n", "10\n"), _spec(), ctx)
    assert v2.status == "rejected"
    assert v2.evidence["reference_stdout"] == "9\n"
    assert v2.evidence["candidate_expected"] == "10\n"


def test_reference_unvalidated_no_examples_capped_provisional():
    """D8: spec with NO examples -> reference compiles but is unvalidated;
    agreement is capped at provisional, disagreement is inconclusive."""
    from autograder.app.testing.oracle.strategies import ReferenceSolutionOracle

    spec = Specification(requirements=_spec().requirements, examples=[])
    oracle = ReferenceSolutionOracle("int main(){/* adds */}")
    prep = oracle.prepare(
        lambda src: {"ok": True, "binary": "/tmp/ref"}, spec,
        run_reference=lambda b, s: _sum_executor(s))
    assert prep["ready"] is True
    assert prep["validated"] is False
    assert prep["examples_total"] == 0 and prep["examples_reproduced"] == 0

    ctx = OracleContext(reference_executor=_sum_executor)
    v = oracle.assess(_tc("T1", "4 5\n", "9\n"), spec, ctx)
    assert v.status == "provisional"
    assert "reference unvalidated" in v.provenance.detail
    assert "no specification examples" in v.provenance.detail
    assert v.evidence["limitation"].startswith("reference unvalidated")

    v2 = oracle.assess(_tc("T2", "4 5\n", "10\n"), spec, ctx)
    assert v2.status == "inconclusive"  # cannot adjudicate vs unvalidated ref


def test_reference_executor_crash_is_inconclusive():
    from autograder.app.testing.oracle.strategies import ReferenceSolutionOracle

    oracle = ReferenceSolutionOracle("src")
    oracle.prepare(
        lambda src: {"ok": True, "binary": "/tmp/ref"}, _spec(),
        run_reference=lambda b, s: _sum_executor(s))
    v = oracle.assess(
        _tc("T1", "4 5\n", "9\n"), _spec(),
        OracleContext(reference_executor=lambda s: None))
    assert v.status == "inconclusive"


# ---------------------------------------------------------------------------
# Instructor provenance: rules identity (version + hash)
# ---------------------------------------------------------------------------

def test_instructor_provenance_records_rules_identity():
    from autograder.app.testing.oracle.strategies import InstructorExpectedOracle

    strat = InstructorExpectedOracle()
    ctx = OracleContext(
        instructor_expected={"7 8\n": "15\n"},
        rules_version="2.3.0", rules_hash="sha256:deadbeef")
    v = strat.assess(_tc("T7", "7 8\n", "15\n"), _spec(), ctx)
    assert v.status == "trusted"
    assert v.evidence["rules_version"] == "2.3.0"
    assert v.evidence["rules_hash"] == "sha256:deadbeef"
    assert "2.3.0" in v.provenance.detail
    assert "sha256:deadbeef" in v.provenance.detail

    # Through the service, the identity reaches the test case provenance.
    svc = OracleService()
    updated, _ = svc.assess(_tc("T7", "7 8\n", "15\n"), _spec(), _rules(), ctx)
    assert updated.status == "trusted"
    assert "2.3.0" in updated.expected.provenance.detail


# ---------------------------------------------------------------------------
# D10: budget enforcement
# ---------------------------------------------------------------------------

def test_token_budget_stops_refinement(mock_ollama):
    """A 1-token budget is blown by the first generation round; the loop
    must stop (no refinement round) and record the limitation."""
    url, server = mock_ollama
    server.script["generate"] = [
        ("json", {"response": json.dumps(CANDIDATES)}),
        ("json", {"response": json.dumps({"candidates": []})}),
    ]
    rules = _rules()
    rules.test_generation.token_budget = 1
    client = OllamaClient(url)
    try:
        summary = generate_test_suite(_spec(), rules, client)
    finally:
        client.close()
    assert summary["rounds_used"] == 1
    assert summary["tokens_used"] > 1
    assert any("token budget exhausted" in lim
               for lim in summary["limitations"])
    # Round-1 candidates were still admitted (budget gates FUTURE rounds).
    assert {tc.id for tc in summary["candidates"]} == {"T1", "T2"}


def test_exec_budget_exhaustion_marks_inconclusive():
    """The single execution slot is spent validating the reference
    against the spec example; the candidate run then has no budget and
    must be inconclusive — never executed anyway, never trusted."""
    from autograder.app.testing.generation.budgets import BudgetTracker
    from autograder.app.testing.oracle.strategies import ReferenceSolutionOracle

    tracker = BudgetTracker(token_budget=100_000, exec_budget=1)
    oracle = ReferenceSolutionOracle("src")
    oracle.set_budget_tracker(tracker)
    prep = oracle.prepare(
        lambda src: {"ok": True, "binary": "/tmp/ref"}, _spec(),
        run_reference=lambda b, s: _sum_executor(s))
    assert prep["validated"] is True
    assert tracker.execs_used == 1

    v = oracle.assess(
        _tc("T1", "4 5\n", "9\n"), _spec(),
        OracleContext(reference_executor=_sum_executor))
    assert v.status == "inconclusive"
    assert "execution budget exhausted" in v.provenance.detail


def test_exec_budget_zero_blocks_example_validation():
    """With zero execution budget the reference cannot validate even one
    example: it stays unvalidated (ready, but capped) — coverage 0."""
    from autograder.app.testing.generation.budgets import BudgetTracker
    from autograder.app.testing.oracle.strategies import ReferenceSolutionOracle

    tracker = BudgetTracker(token_budget=100_000, exec_budget=0)
    oracle = ReferenceSolutionOracle("src")
    oracle.set_budget_tracker(tracker)
    prep = oracle.prepare(
        lambda src: {"ok": True, "binary": "/tmp/ref"}, _spec(),
        run_reference=lambda b, s: _sum_executor(s))
    assert prep["ready"] is True
    assert prep["validated"] is False
    assert prep["examples_run"] == 0
