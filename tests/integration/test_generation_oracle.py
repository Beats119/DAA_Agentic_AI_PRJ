"""Generation + validation + oracle tests — mock LLM, synthetic fixtures.

Covers the full candidate lifecycle: LLM proposes -> deterministic
validation -> tiered oracle assessment. Asserts the critical invariant:
Tier-3 agreement NEVER yields ``trusted``.
"""

from __future__ import annotations

import json

import pytest

from conftest import make_rules

from autograder.app.core.schemas import (
    AssignmentDoc,
    PageText,
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
    rules = _rules()
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
    rules = _rules()
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
