"""Dynamic test generation agent + bounded refinement loop.

Fresh candidates every call — no caching, no memoization. The loop is
bounded by the rules' test-generation policy (rounds, candidate count,
token/exec budgets); it cannot run forever. Budgets are *enforced* (D10):
token usage from every generation record is accumulated into a shared
:class:`BudgetTracker`, and no new round starts once the token budget is
exhausted — exhaustion is reported in the suite's ``limitations``.

Pipeline per round:
  1. LLM proposes candidates (structured JSON -> TestCandidate).
  2. Deterministic validation discards invalid ones (format, contract,
     duplicates).
  3. If requirements remain uncovered and budget remains, one refinement
     round targets them (with quarantine feedback).
  4. Stop at max_rounds, max_candidates, token budget, or full coverage.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ...core.schemas import (
    ExpectedOutput,
    ExpectedProvenance,
    RuleSet,
    Specification,
    TestCandidate,
    TestCase,
)
from ...ollama.client import OllamaClient
from ...ollama.models import model_cfg_for
from ...ollama.prompts import load_prompt
from ..validation.cases import validate_candidate
from .budgets import (
    MAX_CASES_PER_ROUND,
    MIN_CASES_PER_ROUND,
    BudgetTracker,
    effective_budgets,
)


class CandidateList(BaseModel):
    """Wire shape for the generation prompts: a JSON array of candidates."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[TestCandidate]


class GenerationError(RuntimeError):
    """Raised when candidate generation fails irrecoverably."""


def _render_prompt(name: str, **fields: Any) -> tuple[dict, str]:
    fm, body = load_prompt(name)
    for key, value in fields.items():
        body = body.replace("{" + key + "}", str(value))
    return fm, body


def generate_candidates(
    spec: Specification,
    rules: RuleSet,
    client: OllamaClient,
    *,
    task: str = "test_generation",
    min_cases: int = MIN_CASES_PER_ROUND,
    max_cases: int = MAX_CASES_PER_ROUND,
) -> tuple[list[TestCandidate], list[dict[str, Any]]]:
    """One LLM generation round. Returns ``(candidates, records)``.

    ``records`` are the :class:`GenerationRecord` dicts for provenance.
    Raises :class:`GenerationError` if the model cannot produce valid
    candidates after the gateway's own retries.
    """
    from ...specification.agent import specification_to_json

    model_cfg = model_cfg_for(task)
    _, body = _render_prompt(
        "test_generation",
        specification_json=specification_to_json(spec),
        min_cases=min_cases,
        max_cases=max_cases,
    )
    try:
        result, record = client.generate_structured(
            task=task,
            prompt=body,
            schema=CandidateList,
            model_cfg=model_cfg,
            prompt_name="test_generation",
        )
    except Exception as exc:
        raise GenerationError(f"candidate generation failed: {exc}") from exc
    return result.candidates, [record.model_dump(mode="json")]


def candidate_to_testcase(candidate: TestCandidate) -> TestCase:
    """Wrap a validated candidate as a :class:`TestCase`.

    Expected output starts life as an *untrusted LLM proposal*
    (tier3_llm provenance, ``inconclusive`` status). The oracle subsystem —
    and only the oracle subsystem — may promote it.
    """
    return TestCase(
        id=candidate.id,
        input=candidate.input,
        input_category=candidate.input_category,
        rationale=candidate.rationale,
        requirement_refs=list(candidate.requirement_refs),
        expected=ExpectedOutput(
            value=candidate.expected_value,
            provenance=ExpectedProvenance(
                tier="tier3_llm",
                method="llm_proposed_candidate",
                detail="unvalidated model proposal; oracle assessment pending",
            ),
        ),
        status="inconclusive",
        validation_diagnostics={},
    )


def refine_candidates(
    spec: Specification,
    rules: RuleSet,
    client: OllamaClient,
    rejected: list[dict[str, Any]],
    uncovered_requirement_ids: list[str],
    existing_ids: set[str],
    next_id: int,
    *,
    task: str = "test_refinement",
) -> tuple[list[TestCandidate], list[dict[str, Any]]]:
    """One bounded refinement round targeting rejected/uncovered work."""
    import json

    from ...specification.agent import specification_to_json

    budgets = effective_budgets(rules.test_generation)
    max_new = max(0, budgets["max_candidates"] - len(existing_ids))
    if max_new == 0:
        return [], []
    model_cfg = model_cfg_for(task)
    _, body = _render_prompt(
        "test_refinement",
        specification_json=specification_to_json(spec),
        rejected_json=json.dumps(rejected, indent=1)[:6000],
        uncovered_json=json.dumps(uncovered_requirement_ids, indent=1)[:2000],
        max_new=min(max_new, MAX_CASES_PER_ROUND),
        next_id=f"T{next_id}",
    )
    try:
        result, record = client.generate_structured(
            task=task,
            prompt=body,
            schema=CandidateList,
            model_cfg=model_cfg,
            prompt_name="test_refinement",
        )
    except Exception as exc:
        raise GenerationError(f"candidate refinement failed: {exc}") from exc
    fresh = [c for c in result.candidates if c.id not in existing_ids][:max_new]
    return fresh, [record.model_dump(mode="json")]


def generate_test_suite(
    spec: Specification,
    rules: RuleSet,
    client: OllamaClient,
    *,
    budget_tracker: BudgetTracker | None = None,
) -> dict[str, Any]:
    """Full bounded generation pipeline.

    Returns a plain-data summary::

        {
          "candidates": [TestCase, ...],   # validated, deduped
          "quarantined": [{"id","reason",...}],
          "generation_records": [...],
          "rounds_used": int,
          "budgets": {...},
          "uncovered_requirements": [...],
          "limitations": [...],            # e.g. "token budget exhausted"
        }

    ``budget_tracker`` is the per-run counter shared with the oracle and
    correctness phases; when omitted, a fresh one is built from the rules.
    Token usage from each generation record is accumulated into it, and
    the loop stops *before* starting a round that the (already exhausted)
    budget cannot pay for.

    Never raises for "no usable candidates" — that is a legitimate outcome
    reported honestly (the orchestrator then marks missing evidence).
    """
    budgets = effective_budgets(rules.test_generation)
    tracker = budget_tracker or BudgetTracker.from_policy(rules.test_generation)
    quarantined: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    limitations: list[str] = []
    accepted: list[TestCase] = []
    seen_ids: set[str] = set()
    seen_inputs: set[str] = set()

    def _account_tokens(recs: list[dict[str, Any]]) -> None:
        for rec in recs:
            tracker.add_tokens(int(rec.get("tokens_used", 0) or 0))

    def _admit(cands: list[TestCandidate]) -> None:
        for cand in cands:
            if cand.id in seen_ids:
                quarantined.append({"id": cand.id, "reason": "duplicate id"})
                continue
            norm = cand.input.strip()
            if norm in seen_inputs:
                quarantined.append(
                    {"id": cand.id, "reason": "duplicate input of an accepted case"}
                )
                continue
            result = validate_candidate(cand, spec)
            if not result.ok:
                quarantined.append({"id": cand.id, "reason": "; ".join(result.errors)})
                continue
            tc = candidate_to_testcase(cand)
            accepted.append(
                tc.model_copy(
                    update={"validation_diagnostics": {"validated": True}}
                )
            )
            seen_ids.add(cand.id)
            seen_inputs.add(norm)
            if len(accepted) >= budgets["max_candidates"]:
                break

    rounds_used = 0
    if tracker.tokens_exhausted():
        # A shared tracker can arrive pre-exhausted (e.g. earlier phases
        # consumed the budget): do not start a round we cannot pay for.
        limitations.append(
            "token budget exhausted before generation started "
            f"({tracker.tokens_used}/{budgets['token_budget']} tokens used)"
        )
    else:
        initial, recs = generate_candidates(spec, rules, client)
        records.extend(recs)
        _account_tokens(recs)
        rounds_used += 1
        _admit(initial)
        if tracker.tokens_used > budgets["token_budget"]:
            limitations.append(
                "token budget exhausted "
                f"({tracker.tokens_used}/{budgets['token_budget']} tokens used); "
                "no further generation rounds"
            )

    next_num = len(seen_ids) + 1
    while (
        rounds_used < budgets["max_rounds"]
        and len(accepted) < budgets["max_candidates"]
        and rounds_used > 0
    ):
        if tracker.tokens_exhausted():
            if not any("token budget exhausted" in lim for lim in limitations):
                limitations.append(
                    "token budget exhausted "
                    f"({tracker.tokens_used}/{budgets['token_budget']} tokens used); "
                    "refinement stopped"
                )
            break
        covered = {r for tc in accepted for r in tc.requirement_refs}
        uncovered = [r.id for r in spec.requirements if r.id not in covered]
        if not uncovered and quarantined_reason_free(quarantined):
            break
        refined, recs = refine_candidates(
            spec, rules, client,
            rejected=quarantined[-8:],
            uncovered_requirement_ids=uncovered,
            existing_ids=seen_ids,
            next_id=next_num,
        )
        records.extend(recs)
        _account_tokens(recs)
        rounds_used += 1
        before = len(accepted)
        _admit(refined)
        next_num = len(seen_ids) + 1
        if tracker.tokens_used > budgets["token_budget"]:
            limitations.append(
                "token budget exhausted "
                f"({tracker.tokens_used}/{budgets['token_budget']} tokens used); "
                "no further generation rounds"
            )
            break
        if len(accepted) == before:
            break  # refinement produced nothing usable; stop, don't spin

    covered = {r for tc in accepted for r in tc.requirement_refs}
    return {
        "candidates": accepted,
        "quarantined": quarantined,
        "generation_records": records,
        "rounds_used": rounds_used,
        "budgets": budgets,
        "uncovered_requirements": [r.id for r in spec.requirements if r.id not in covered],
        "limitations": limitations,
        "tokens_used": tracker.tokens_used,
    }


def quarantined_reason_free(quarantined: list[dict[str, Any]]) -> bool:
    """True when there is nothing worth a refinement round."""
    return not quarantined


__all__ = [
    "CandidateList",
    "GenerationError",
    "candidate_to_testcase",
    "generate_candidates",
    "generate_test_suite",
    "refine_candidates",
]
