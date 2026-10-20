"""Oracle service: tier-ordered assessment with full provenance.

For each candidate test case, strategies run in the rules' tier-preference
order (default: tier1_reference → tier2_independent → tier3_llm). The first
non-``inconclusive`` verdict wins; the full chain (every strategy's verdict)
is recorded so the report can explain *why* a test is trusted, provisional,
rejected, or inconclusive.

Instructor ``expected_outputs`` (from the rules config) are Tier-1 evidence:
they are authoritative for the instructor's own assignment and are labeled
exactly as such — they never replace fresh LLM generation, they only
adjudicate the candidates it produced.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ...core.schemas import (
    ExpectedOutput,
    OracleStatus,
    OracleVerdict,
    RuleSet,
    Specification,
    TestCase,
)
from .strategies import (
    INSTRUCTOR,
    TIER1,
    TIER2,
    TIER3,
    LLMProposedOracle,
    OracleContext,
    OracleStrategy,
    ReferenceSolutionOracle,
    SpecExampleOracle,
    InstructorExpectedOracle,
)

log = logging.getLogger("autograder.workflow")

_TIER_ORDER = [INSTRUCTOR, TIER1, TIER2, TIER3]

# Map the rules' tier_preference literals to strategy tiers.
_PREF_MAP = {
    "tier1_reference": [INSTRUCTOR, TIER1],
    "tier2_independent": [TIER2],
    "tier3_llm": [TIER3],
}


class OracleService:
    """Assess test cases through the tiered oracle hierarchy."""

    def __init__(
        self,
        strategies: list[OracleStrategy] | None = None,
        reference_source: str | None = None,
    ) -> None:
        if strategies is None:
            strategies = [
                InstructorExpectedOracle(),
                ReferenceSolutionOracle(reference_source),
                SpecExampleOracle(),
                LLMProposedOracle(),
            ]
        self.strategies = strategies

    def _ordered(self, rules: RuleSet) -> list[OracleStrategy]:
        wanted: list[str] = []
        for pref in rules.oracle.tier_preference:
            wanted.extend(_PREF_MAP.get(pref, []))
        # Strategies not covered by the preference keep their relative order
        # at the end (never silently dropped).
        ordered = [s for tier in wanted for s in self.strategies if s.tier == tier]
        ordered += [s for s in self.strategies if s not in ordered]
        # De-duplicate while preserving order.
        seen: set[int] = set()
        out: list[OracleStrategy] = []
        for s in ordered:
            if id(s) not in seen:
                seen.add(id(s))
                out.append(s)
        return out

    def assess(
        self,
        test_case: TestCase,
        spec: Specification,
        rules: RuleSet,
        ctx: OracleContext,
    ) -> tuple[TestCase, list[dict[str, Any]]]:
        """Assess one case. Returns ``(updated_test_case, provenance_chain)``.

        The test case's ``expected``/``status`` are replaced by the winning
        verdict's. ``provenance_chain`` records every strategy consulted.
        """
        chain: list[dict[str, Any]] = []
        winner: OracleVerdict | None = None
        for strategy in self._ordered(rules):
            verdict = strategy.assess(test_case, spec, ctx)
            chain.append(
                {
                    "strategy": strategy.name,
                    "tier": strategy.tier,
                    "status": verdict.status,
                    "method": verdict.provenance.method,
                    "detail": verdict.provenance.detail,
                }
            )
            if verdict.status != "inconclusive":
                winner = verdict
                break
        if winner is None:
            # Every strategy abstained: stay inconclusive, honestly.
            winner = OracleVerdict(
                status="inconclusive",
                provenance=test_case.expected.provenance,
                evidence={"note": "no oracle strategy produced a verdict"},
            )
        updated = test_case.model_copy(
            update={
                "expected": ExpectedOutput(
                    value=test_case.expected.value,
                    provenance=winner.provenance,
                ),
                "status": winner.status,
                "validation_diagnostics": {
                    **test_case.validation_diagnostics,
                    "oracle_chain": chain,
                    "oracle_evidence": winner.evidence,
                },
            }
        )
        return updated, chain

    def assess_all(
        self,
        cases: list[TestCase],
        spec: Specification,
        rules: RuleSet,
        ctx: OracleContext,
    ) -> tuple[list[TestCase], dict[OracleStatus, int], list[dict[str, Any]]]:
        """Assess every case. Returns ``(cases, status_counts, chains)``."""
        t0 = time.monotonic()
        out: list[TestCase] = []
        chains: list[dict[str, Any]] = []
        counts: dict[OracleStatus, int] = {
            "trusted": 0,
            "provisional": 0,
            "rejected": 0,
            "inconclusive": 0,
        }
        for tc in cases:
            updated, chain = self.assess(tc, spec, rules, ctx)
            out.append(updated)
            chains.append({"test_id": tc.id, "chain": chain})
            counts[updated.status] += 1
        log.info("oracle assessment: %d cases in %.1fs "
                 "(trusted=%d, provisional=%d, rejected=%d, inconclusive=%d)",
                 len(cases), time.monotonic() - t0,
                 counts["trusted"], counts["provisional"],
                 counts["rejected"], counts["inconclusive"])
        return out, counts, chains
