"""Prompt-template contracts: loading, placeholders, examples, size budget.

The templates in ``prompts/*.txt`` are the only instruction channel to the
local model, and callers substitute ``{placeholder}`` tokens with plain
``str.replace`` — a missing placeholder silently sends the literal token to
the model, and a malformed few-shot example teaches malformed output. These
tests pin both, plus the schema validity of every shipped example and a
size budget so the templates cannot quietly bloat local inference latency.
"""

from __future__ import annotations

import json
import re

import pytest

from autograder.app.analyzers.readability.analyzer import CodeReview
from autograder.app.core.schemas import Specification
from autograder.app.ollama.prompts import load_prompt
from autograder.app.orchestration.feedback import FeedbackText
from autograder.app.testing.generation.agent import CandidateList

ALL_TEMPLATES = [
    "specification",
    "test_generation",
    "test_refinement",
    "code_review",
    "feedback",
    "feedback_check",
    "oracle_verify",
]

# Placeholders each calling module substitutes via str.replace, derived
# from the callers: specification/agent.py, testing/generation/agent.py
# (_render_prompt fields), analyzers/readability/analyzer.py,
# orchestration/feedback.py, testing/oracle/strategies.py.
PLACEHOLDERS = {
    "specification": {"{assignment_text}"},
    "test_generation": {"{specification_json}", "{min_cases}", "{max_cases}"},
    "test_refinement": {
        "{specification_json}",
        "{rejected_json}",
        "{uncovered_json}",
        "{max_new}",
        "{next_id}",
    },
    "code_review": {"{source_code}"},
    "feedback": {"{evidence_json}"},
    "feedback_check": {"{feedback_json}", "{evidence_json}"},
    "oracle_verify": {"{specification_text}", "{test_input}"},
}

# Templates carrying a few-shot example, mapped to the exact Pydantic
# schema production code validates the model's reply against.
EXAMPLE_SCHEMAS = {
    "specification": Specification,
    "test_generation": CandidateList,
    "test_refinement": CandidateList,
    "code_review": CodeReview,
    "feedback": FeedbackText,
}

BREVITY_MARKER = "Be concise"
MAX_BODY_CHARS = 4000

_EVIDENCE_ID_RE = re.compile(r"ev:[A-Za-z0-9_-]+:\d+")


def _example_payload(body: str) -> dict:
    """Parse the JSON literal on the first line after 'Example output:'."""
    tail = body.split("Example output:", 1)[1]
    line = next(ln.strip() for ln in tail.splitlines() if ln.strip())
    return json.loads(line)


@pytest.mark.parametrize("name", ALL_TEMPLATES)
def test_template_loads_with_version(name: str) -> None:
    front_matter, body = load_prompt(name)
    assert front_matter.get("version"), f"{name}: frontmatter has no version"
    assert body.strip(), f"{name}: empty body"


@pytest.mark.parametrize("name", ALL_TEMPLATES)
def test_caller_placeholders_present(name: str) -> None:
    _, body = load_prompt(name)
    missing = {tok for tok in PLACEHOLDERS[name] if tok not in body}
    assert not missing, f"{name}: missing placeholders {sorted(missing)}"


@pytest.mark.parametrize("name", ALL_TEMPLATES)
def test_brevity_instruction_present(name: str) -> None:
    _, body = load_prompt(name)
    assert BREVITY_MARKER in body, f"{name}: no explicit brevity instruction"


@pytest.mark.parametrize("name", ALL_TEMPLATES)
def test_body_within_size_budget(name: str) -> None:
    _, body = load_prompt(name)
    assert len(body) <= MAX_BODY_CHARS, (
        f"{name}: body is {len(body)} chars (budget {MAX_BODY_CHARS})"
    )


@pytest.mark.parametrize("name", sorted(EXAMPLE_SCHEMAS))
def test_example_output_validates_against_schema(name: str) -> None:
    _, body = load_prompt(name)
    payload = _example_payload(body)
    instance = EXAMPLE_SCHEMAS[name].model_validate(payload)
    assert instance is not None


def test_feedback_example_cites_only_input_evidence_ids() -> None:
    """The feedback example must not teach inventing evidence IDs."""
    _, body = load_prompt("feedback")
    excerpt = body.split("Example input (excerpt):", 1)[1]
    excerpt = excerpt.split("Example output:", 1)[0]
    input_ids = set(_EVIDENCE_ID_RE.findall(excerpt))
    assert input_ids, "feedback template: example input cites no evidence ID"
    payload = _example_payload(body)
    cited = {failure["evidence_id"] for failure in payload["per_failure"]}
    assert cited, "feedback example: no per-failure citation to check"
    assert cited <= input_ids, (
        f"feedback example cites IDs absent from its input: {cited - input_ids}"
    )
