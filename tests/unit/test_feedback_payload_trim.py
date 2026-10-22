"""Unit tests: the feedback generate prompt carries a citable subset.

The full evidence registry is re-read by the local model on every
feedback draft attempt, so ``finalize_feedback`` now builds its prompt
payload with :func:`build_feedback_prompt_payload`:

- confirmed-failure ``test_result`` records are kept; passing outcomes
  are counted, not serialized;
- other records stay, with raw blobs stripped and long strings capped;
- id-less context entries carry metric summaries, outcome counts, and
  the run's limitations.

The deterministic verifier and the opt-in LLM audit keep operating on
the FULL registry — proven here by a draft that cites a confirmed
failure and verifies identically through ``finalize_feedback``.
"""

from __future__ import annotations

import json

from autograder.app.core.schemas import GenerationRecord
from autograder.app.orchestration.evidence import EvidenceRegistry
from autograder.app.orchestration.feedback import (
    FeedbackFailure,
    FeedbackText,
    build_feedback_prompt_payload,
    build_trusted_results,
    finalize_feedback,
    verify_feedback,
)

_PROVENANCE = {
    "tier": "tier1_reference",
    "method": "reference_run",
    "detail": "reference solution executed in the sandbox",
}


def _test_result(tid: str, passed: bool, actual: str) -> dict:
    return {
        "id": tid,
        "input": "2 3",
        "expected": "5",
        "actual": actual,
        "passed": passed,
        "outcome": "run_ok",
        "oracle_provenance": dict(_PROVENANCE),
    }


def _registry() -> tuple[EvidenceRegistry, dict[str, str]]:
    """A registry shaped like a real run's: many passing outcomes with
    bulky outputs, two confirmed failures, and bulky analyzer records."""
    reg = EvidenceRegistry("trimrun")
    ids: dict[str, str] = {}
    ids["passing"] = [
        reg.append("test_result", "correctness",
                   _test_result(f"T{i}", True, "5\n" + "p" * 3000))
        for i in range(1, 11)
    ]
    ids["fail_a"] = reg.append(
        "test_result", "correctness", _test_result("T11", False, "6"))
    ids["fail_b"] = reg.append(
        "test_result", "correctness",
        _test_result("T12", False, "9" * 5000))
    ids["infra"] = reg.append(
        "test_infra_error", "correctness",
        {"id": "T13", "outcome": "infra_error", "raw_stderr": "e" * 4000})
    ids["sanitizer"] = reg.append(
        "sanitizer_findings", "memory_safety",
        {"finding_count": 2,
         "findings": [
             {"type": "heap-buffer-overflow", "raw_tail": "s" * 1500},
             {"type": "memory-leak", "raw_tail": "s" * 1500}],
         "per_test": [{"test_id": "T1", "raw_tail": "s" * 1500}]})
    ids["compile"] = reg.append(
        "compile_result", "compiler",
        {"ok": True, "diagnostics": ["warning: unused variable"],
         "raw_stderr_tail": "w" * 2000})
    ids["generated"] = reg.append(
        "tests_generated", "test_generation",
        {"accepted": 12,
         "quarantined": [
             {"id": f"Q{i}", "input": "q" * 500, "expected_value": "0",
              "rationale": "quarantined candidate"}
             for i in range(5)],
         "rounds_used": 2, "uncovered_requirements": ["R9"]})
    ids["sandbox"] = reg.append(
        "sandbox_capabilities", "sandbox", {"detail": "c" * 2000})
    ids["oracle"] = reg.append(
        "oracle_assessment", "oracle",
        {"status_counts": {"trusted": 12, "rejected": 5},
         "chains": [{"test_id": "T1",
                     "verdicts": [["instructor_expected", "instructor",
                                   "trusted"]]}]})
    reg.finalize()
    return reg, ids


def _payload(reg: EvidenceRegistry) -> tuple[str, dict, list]:
    trusted, failing = build_trusted_results(reg)
    payload = build_feedback_prompt_payload(
        reg.to_json(),
        trusted_results=trusted,
        failing_tests=failing,
        metric_scores={"correctness": 0.83, "compiler": 1.0,
                       "memory_safety": None},
        analyzer_statuses={"correctness": "completed",
                           "compiler": "completed",
                           "memory_safety": "completed"},
        uncertainty_notes=["requirement R9 had no trusted test"],
    )
    return payload, trusted, failing


def test_payload_is_materially_smaller_than_full_registry():
    reg, _ = _registry()
    payload, _, _ = _payload(reg)
    full = reg.to_json()
    assert len(payload) < 0.4 * len(full), (
        f"payload {len(payload)} chars vs full registry {len(full)} chars"
    )


def test_confirmed_failures_and_citable_entries_survive():
    reg, ids = _registry()
    payload, _, _ = _payload(reg)
    # Both confirmed failures keep their ids and citation fields.
    for key in ("fail_a", "fail_b", "infra", "sanitizer", "compile",
                "generated", "oracle"):
        assert ids[key] in payload, f"{key} evidence id missing"
    parsed = {r.get("id"): r for r in json.loads(payload) if "id" in r}
    fail_a = parsed[ids["fail_a"]]["data"]
    assert fail_a["input"] == "2 3"
    assert fail_a["expected"] == "5"
    assert fail_a["actual"] == "6"
    assert fail_a["oracle_provenance"]["tier"] == "tier1_reference"


def test_passing_outcomes_are_counted_not_serialized():
    reg, ids = _registry()
    payload, _, _ = _payload(reg)
    for pid in ids["passing"]:
        assert f'"{pid}"' not in payload  # quoted: seq 1 vs seq 11 prefix
    parsed = json.loads(payload)
    summary = next(r for r in parsed if r["kind"] == "test_outcome_summary")
    assert summary == {"kind": "test_outcome_summary", "trusted_tests": 12,
                       "passed": 10, "failed": 2}


def test_bulky_fields_stripped_and_strings_capped():
    reg, ids = _registry()
    payload, _, _ = _payload(reg)
    assert "s" * 100 not in payload          # sanitizer raw_tail dropped
    assert "e" * 100 not in payload          # infra raw_stderr dropped
    assert "w" * 900 not in payload          # raw_stderr_tail capped at 800
    assert "c" * 900 not in payload          # generic string cap applies
    parsed = {r.get("id"): r for r in json.loads(payload) if "id" in r}
    # A failing test's actual output keeps its head, bounded near the cap.
    actual = parsed[ids["fail_b"]]["data"]["actual"]
    assert actual.startswith("9" * 100)
    assert len(actual) <= 2100
    # Quarantined candidates collapse to a count.
    generated = parsed[ids["generated"]]["data"]
    assert generated["quarantined"] == {"count": 5}
    assert generated["uncovered_requirements"] == ["R9"]


def test_metric_summary_and_limitations_context_entries():
    reg, _ = _registry()
    payload, _, _ = _payload(reg)
    parsed = json.loads(payload)
    metrics = next(r for r in parsed if r["kind"] == "metric_summary")
    by_name = {m["name"]: m for m in metrics["metrics"]}
    assert by_name["correctness"] == {
        "name": "correctness", "status": "completed", "score": 0.83}
    assert by_name["memory_safety"]["score"] is None
    limitations = next(r for r in parsed if r["kind"] == "run_limitations")
    assert limitations["limitations"] == [
        "requirement R9 had no trusted test"]
    # Context entries carry no evidence id — they can never be cited.
    for entry in parsed:
        if entry["kind"] in ("metric_summary", "run_limitations",
                             "test_outcome_summary"):
            assert "id" not in entry


def test_small_registry_shape_unchanged_without_context():
    reg = EvidenceRegistry("small")
    reg.append("test_result", "correctness",
               _test_result("T1", False, "6"))
    reg.finalize()
    payload = build_feedback_prompt_payload(reg.to_json())
    assert json.loads(payload) == json.loads(reg.to_json())


def test_unfamiliar_payloads_pass_through_verbatim():
    assert build_feedback_prompt_payload("not json") == "not json"
    assert build_feedback_prompt_payload('{"a": 1}') == '{"a": 1}'
    assert build_feedback_prompt_payload("[1, 2]") == "[1, 2]"


class _DraftGateway:
    """TEST-ONLY gateway: returns a fixed draft citing a real failure."""

    def __init__(self, draft: FeedbackText) -> None:
        self.draft = draft
        self.prompts: list[str] = []

    def generate_structured(self, *, task, prompt, schema, model_cfg,
                            prompt_name=None, **kw):
        self.prompts.append(prompt)
        record = GenerationRecord(
            task=task, model="mock-model", temperature=0.0, seed=42,
            timeout_s=120.0, prompt_version="1.0",
            prompt_hash="sha256:" + "0" * 16, duration_s=0.01)
        return self.draft, record


def test_finalize_uses_trimmed_payload_and_verifies_against_full_registry():
    reg, ids = _registry()
    trusted, failing = build_trusted_results(reg)
    draft = FeedbackText(
        summary="Two trusted tests failed; the rest passed.",
        per_failure=[FeedbackFailure(
            evidence_id=ids["fail_a"],
            explanation=(
                "On input '2 3' the trusted expected output is '5' "
                "(oracle: tier tier1_reference, reference_run), but the "
                "program produced '6'."),
            suggestion="Check the addition path for the second operand.",
        )],
    )
    # The draft verifies against the FULL registry, exactly as before.
    result = verify_feedback(draft, reg, trusted, None,
                             {"compiler": "completed"})
    assert result.ok, result.violations

    gateway = _DraftGateway(draft)
    out = finalize_feedback(
        evidence_json=reg.to_json(),
        gateway=gateway,
        registry=reg,
        trusted_results=trusted,
        grade=None,
        analyzer_statuses={"compiler": "completed"},
        failing_tests=failing,
        analyzer_notes=[],
        uncertainty_notes=["requirement R9 had no trusted test"],
        test_stats={"generated": 17, "trusted": 12},
        run_status="completed_with_warnings",
        metric_scores={"correctness": 0.83},
    )
    assert out["used_fallback"] is False
    assert out["attempts"] == 1
    (prompt,) = gateway.prompts
    # The model saw the trimmed payload: the failure and the context…
    assert ids["fail_a"] in prompt
    assert "test_outcome_summary" in prompt
    # …and none of the passing outcomes' evidence ids or raw blobs.
    assert all(f'"{pid}"' not in prompt for pid in ids["passing"])
    assert "s" * 100 not in prompt
