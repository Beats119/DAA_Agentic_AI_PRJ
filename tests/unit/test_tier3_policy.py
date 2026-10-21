"""Tier-3 oracle policy (opt-in, default OFF) + budget accounting.

Findings 1-2 of the latency remediation: Tier-3 costs one LLM call per
otherwise-inconclusive candidate and can never produce trust, so it is
an instructor opt-in. When off, the skip is recorded honestly in the
verdict chain. When on, its calls draw from the same shared run token
budget as test generation, checked BEFORE each call, and its records
are collected for the manifest.
"""

from __future__ import annotations

from typing import Any

from conftest import make_rules

from autograder.app.core.schemas import (
    ExpectedOutput,
    ExpectedProvenance,
    GenerationRecord,
    Requirement,
    Specification,
    TestCase,
)
from autograder.app.testing.generation.budgets import BudgetTracker
from autograder.app.testing.oracle.service import OracleService
from autograder.app.testing.oracle.strategies import (
    OracleContext,
    SecondPathAnswer,
)

TOKENS_PER_CALL = 37


class CountingGateway:
    """TEST-ONLY gateway: counts calls, always agrees with candidates."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate_structured(self, *, task: str, prompt: str, schema: type,
                            model_cfg: dict[str, Any], prompt_name: str | None = None,
                            validation_retries: int = 2):
        self.calls.append(task)
        record = GenerationRecord(
            task=task, model="fake-test-model", temperature=0.0, seed=43,
            timeout_s=5.0, prompt_version="test-1.0",
            prompt_hash="sha256:" + "b" * 16, duration_s=0.01,
            tokens_used=TOKENS_PER_CALL, output_tokens=11,
            prompt_tokens=26, validation_passed=True, attempts=1,
        )
        assert schema is SecondPathAnswer
        # Agree with whatever the candidate proposed: the prompt does not
        # carry it, so echo via the test's hook below.
        return SecondPathAnswer(
            reasoning="fake", expected_output=self._agree_with), record

    _agree_with = ""


def _spec() -> Specification:
    return Specification(
        requirements=[
            Requirement(id="R1", text="Read values and print a result.",
                        kind="explicit"),
        ],
        examples=[],
    )


def _case(cid: str, inp: str, expected: str) -> TestCase:
    return TestCase(
        id=cid, input=inp, input_category="normal", rationale="r",
        requirement_refs=["R1"],
        expected=ExpectedOutput(
            value=expected,
            provenance=ExpectedProvenance(
                tier="tier3_llm", method="llm_proposed_candidate",
                detail="unvalidated model proposal")),
        status="inconclusive",
    )


def _rules(**oracle_over):
    base = {"tier_preference": ["tier1_reference", "tier2_independent",
                                 "tier3_llm"]}
    base.update(oracle_over)
    return make_rules([("correctness", "correctness", 1.0)], oracle=base)


def _cases() -> list[TestCase]:
    return [_case("T1", "1 2", "3"), _case("T2", "4 5", "9"),
            _case("T3", "7 8", "15")]


def _skip_entries(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in chain
            if "tier3_skipped_by_policy" in str(e.get("detail", ""))]


# --------------------------------------------------------------------- #
# Finding 1: default OFF, honestly recorded
# --------------------------------------------------------------------- #

def test_tier3_off_by_default_makes_zero_llm_calls():
    gateway = CountingGateway()
    svc = OracleService()
    ctx = OracleContext(gateway=gateway)
    updated, counts, chains = svc.assess_all(_cases(), _spec(), _rules(), ctx)
    assert gateway.calls == []
    assert counts["inconclusive"] == 3
    assert counts["trusted"] == 0
    for entry in chains:
        skips = _skip_entries(entry["chain"])
        assert len(skips) == 1
        assert skips[0]["strategy"] == "llm_proposed"
        assert skips[0]["tier"] == "tier3_llm"
        assert skips[0]["status"] == "inconclusive"


def test_tier3_skip_is_in_the_serialized_verdict_chain():
    """The chain the report consumes (on the TestCase) carries the skip."""
    gateway = CountingGateway()
    svc = OracleService()
    ctx = OracleContext(gateway=gateway)
    updated, _chain = svc.assess(_cases()[0], _spec(), _rules(), ctx)
    serialized = updated.validation_diagnostics["oracle_chain"]
    skips = _skip_entries(serialized)
    assert len(skips) == 1
    # A skip is never dressed up as a verification outcome.
    assert updated.status == "inconclusive"


def test_old_rules_without_the_flag_load_with_tier3_off():
    """Backward compatibility: pre-flag rules files get the new default."""
    rules = make_rules([("correctness", "correctness", 1.0)])
    assert rules.oracle.tier3_llm_verification is False


# --------------------------------------------------------------------- #
# Finding 1: opt-in behavior + the never-trusted invariant
# --------------------------------------------------------------------- #

def test_tier3_opt_in_runs_and_agreement_is_only_provisional():
    gateway = CountingGateway()
    svc = OracleService()
    ctx = OracleContext(gateway=gateway)
    rules = _rules(tier3_llm_verification=True)
    cases = _cases()
    for case in cases:
        gateway._agree_with = case.expected.value
        updated, chain = svc.assess(case, _spec(), rules, ctx)
        # The invariant, through the service's status mapping: even a
        # fully agreeing second path yields provisional, NEVER trusted.
        assert updated.status == "provisional"
        assert updated.status != "trusted"
        assert updated.expected.provenance.tier == "tier3_llm"
        real = [e for e in chain if e["strategy"] == "llm_proposed"]
        assert real and "tier3_skipped_by_policy" not in real[0]["detail"]
    assert gateway.calls == ["oracle_second_path"] * 3


def test_tier3_opt_in_records_are_collected_by_the_service():
    gateway = CountingGateway()
    svc = OracleService()
    ctx = OracleContext(gateway=gateway)
    gateway._agree_with = "3"
    svc.assess(_cases()[0], _spec(), _rules(tier3_llm_verification=True), ctx)
    assert len(svc.records) == 1
    assert svc.records[0]["task"] == "oracle_second_path"
    assert svc.records[0]["tokens_used"] == TOKENS_PER_CALL


# --------------------------------------------------------------------- #
# Finding 2: shared budget — checked before, charged after
# --------------------------------------------------------------------- #

def test_tier3_tokens_are_charged_to_the_shared_tracker():
    gateway = CountingGateway()
    tracker = BudgetTracker(token_budget=100_000, exec_budget=10)
    svc = OracleService()
    ctx = OracleContext(gateway=gateway, budget_tracker=tracker)
    gateway._agree_with = "3"
    svc.assess(_cases()[0], _spec(), _rules(tier3_llm_verification=True), ctx)
    assert tracker.tokens_used == TOKENS_PER_CALL


def test_tier3_budget_exhausted_skips_before_calling():
    gateway = CountingGateway()
    tracker = BudgetTracker(token_budget=100, exec_budget=10)
    tracker.add_tokens(100)  # exhausted
    svc = OracleService()
    ctx = OracleContext(gateway=gateway, budget_tracker=tracker)
    updated, chain = svc.assess(
        _cases()[0], _spec(), _rules(tier3_llm_verification=True), ctx)
    assert gateway.calls == []
    assert updated.status == "inconclusive"
    tier3 = [e for e in chain if e["strategy"] == "llm_proposed"]
    assert tier3 and "budget_exhausted" in tier3[0]["detail"]
    assert svc.records == []
