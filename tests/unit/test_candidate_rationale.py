"""Unit tests: candidate rationales are clipped, never rejected.

Nothing in grading reads a candidate's ``rationale``; at local
inference speeds its tokens are pure generation cost. The generation
prompts ask for one short phrase, and ``TestCandidate`` truncates
anything longer to ``RATIONALE_MAX_CHARS`` at the schema boundary —
an over-long rationale must stay a VALID candidate (no validation
failure, no wasted regeneration round), just with a clipped note.
"""

from __future__ import annotations

import json

from autograder.app.core.schemas import RATIONALE_MAX_CHARS, TestCandidate
from autograder.app.testing.generation.agent import (
    CandidateList,
    candidate_to_testcase,
)


def _candidate(rationale: str) -> TestCandidate:
    return TestCandidate(
        id="T1", input="2 3\n", input_category="normal",
        rationale=rationale, requirement_refs=["R1"], expected_value="5\n",
    )


def test_overlong_rationale_is_truncated_not_rejected():
    candidate = _candidate("x" * 500)
    assert len(candidate.rationale) == RATIONALE_MAX_CHARS == 140
    assert candidate.rationale == "x" * 140


def test_wire_payload_with_overlong_rationale_validates():
    payload = {"candidates": [{
        "id": "T1", "input": "2 3\n", "input_category": "normal",
        "rationale": "word " * 120, "requirement_refs": ["R1"],
        "expected_value": "5\n",
    }]}
    result = CandidateList.model_validate_json(json.dumps(payload))
    assert len(result.candidates) == 1
    assert len(result.candidates[0].rationale) == RATIONALE_MAX_CHARS


def test_short_and_exact_cap_rationales_unchanged():
    assert _candidate("boundary probe").rationale == "boundary probe"
    exact = "y" * RATIONALE_MAX_CHARS
    assert _candidate(exact).rationale == exact
    assert _candidate("").rationale == ""


def test_truncation_flows_into_the_testcase():
    case = candidate_to_testcase(_candidate("z" * 500))
    assert len(case.rationale) == RATIONALE_MAX_CHARS
