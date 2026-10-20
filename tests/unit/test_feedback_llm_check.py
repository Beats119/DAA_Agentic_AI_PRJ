"""Optional LLM feedback audit (feedback_check): opt-in supplementary pass.

The deterministic verifier is authoritative. The LLM audit — enabled via
``finalize_feedback(..., llm_check=True)`` — may only OBJECT to a draft
that already passed deterministic verification; objections trigger the
same bounded regeneration, and an audit failure never rejects a verified
draft. The gateway here is a clearly-labeled in-test fake.
"""

from autograder.app.core.schemas import GenerationRecord
from autograder.app.orchestration.evidence import EvidenceRegistry
from autograder.app.orchestration.feedback import (
    FeedbackCheckResult,
    FeedbackCheckViolation,
    FeedbackFailure,
    FeedbackText,
    check_feedback_llm,
    finalize_feedback,
)


def _record(task):
    return GenerationRecord(
        task=task,
        model="mock-model",
        temperature=0.0,
        seed=42,
        timeout_s=120.0,
        prompt_version="1.0",
        prompt_hash="sha256:" + "0" * 16,
        duration_s=0.01,
    )


class FakeGateway:
    """TEST-ONLY gateway: grounded feedback drafts; scripted audit outcomes.

    ``check_outcomes`` is a list consumed one per feedback_check call:
    "pass", "fail", or "raise". Defaults to "pass" when exhausted.
    """

    def __init__(self, check_outcomes=()):
        self.check_outcomes = list(check_outcomes)
        self.tasks: list[str] = []
        self._evidence_id = None

    def generate_structured(self, *, task, prompt, schema, model_cfg,
                            prompt_name=None, **kw):
        self.tasks.append(task)
        if task == "feedback":
            draft = FeedbackText(
                summary="One trusted test failed.",
                per_failure=[
                    FeedbackFailure(
                        evidence_id=self._evidence_id,
                        explanation="Test t1 failed: input '2 3', "
                                    "expected '5', actual '6'.",
                        suggestion="Trace the failing input by hand.",
                    )
                ],
            )
            return draft, _record(task)
        if task == "feedback_check":
            outcome = self.check_outcomes.pop(0) if self.check_outcomes else "pass"
            if outcome == "raise":
                raise RuntimeError("audit model unreachable")
            if outcome == "fail":
                return FeedbackCheckResult(
                    verdict="fail",
                    violations=[
                        FeedbackCheckViolation(
                            claim="One trusted test failed.",
                            reason="scripted objection",
                        )
                    ],
                ), _record(task)
            return FeedbackCheckResult(verdict="pass", violations=[]), _record(task)
        raise AssertionError(f"unexpected task {task!r}")


def _setup(check_outcomes=()):
    registry = EvidenceRegistry("fbcheck")
    good_id = registry.append(
        "test_result", "correctness",
        {"test_id": "t1", "passed": False, "input": "2 3",
         "expected": "5", "actual": "6"},
    )
    registry.finalize()
    gateway = FakeGateway(check_outcomes)
    gateway._evidence_id = good_id
    kwargs = dict(
        evidence_json=registry.to_json(),
        gateway=gateway,
        registry=registry,
        trusted_results={"t1": {"passed": False, "input": "2 3",
                                "expected": "5", "actual": "6",
                                "evidence_id": good_id}},
        grade=None,
        analyzer_statuses={"compiler": "failed"},
        failing_tests=[],
        analyzer_notes=[],
        uncertainty_notes=[],
        test_stats={"generated": 1, "trusted": 1},
        run_status="completed_with_warnings",
    )
    return gateway, kwargs


def test_check_disabled_by_default():
    gateway, kwargs = _setup(check_outcomes=["fail"])
    out = finalize_feedback(**kwargs)
    assert out["used_fallback"] is False
    assert out["llm_check"] == {
        "enabled": False, "status": "disabled", "violations": [],
    }
    # The audit was never invoked.
    assert "feedback_check" not in gateway.tasks


def test_check_pass_accepts_verified_draft():
    gateway, kwargs = _setup(check_outcomes=["pass"])
    out = finalize_feedback(llm_check=True, **kwargs)
    assert out["used_fallback"] is False
    assert out["attempts"] == 1
    assert out["llm_check"]["status"] == "passed"
    assert gateway.tasks.count("feedback_check") == 1


def test_check_objection_triggers_bounded_regeneration():
    gateway, kwargs = _setup(check_outcomes=["fail", "pass"])
    out = finalize_feedback(llm_check=True, **kwargs)
    # First draft objected to -> regenerated -> second draft passes.
    assert out["used_fallback"] is False
    assert out["attempts"] == 2
    assert out["llm_check"]["status"] == "passed"
    assert gateway.tasks.count("feedback_check") == 2


def test_check_persistent_objection_falls_back():
    gateway, kwargs = _setup(check_outcomes=["fail", "fail", "fail"])
    out = finalize_feedback(llm_check=True, max_retries=1, **kwargs)
    assert out["used_fallback"] is True
    assert out["attempts"] == 2  # bounded: 1 initial + 1 retry
    assert out["llm_check"]["status"] == "violations"
    assert any("scripted objection" in v for v in out["violations"])


def test_check_unavailable_accepts_verified_draft():
    # The audit is supplementary: its failure must NOT reject a draft the
    # deterministic verifier accepted.
    gateway, kwargs = _setup(check_outcomes=["raise"])
    out = finalize_feedback(llm_check=True, **kwargs)
    assert out["used_fallback"] is False
    assert out["attempts"] == 1
    assert out["llm_check"]["status"] == "unavailable"


def test_check_feedback_llm_violation_shape():
    gateway, kwargs = _setup(check_outcomes=["fail"])
    draft = FeedbackText(summary="One trusted test failed.")
    violations, info = check_feedback_llm(
        draft, kwargs["evidence_json"], gateway
    )
    assert len(violations) == 1
    assert "scripted objection" in violations[0]
    assert info["task"] == "feedback_check"
    assert info["model"]  # resolved from configs/models.yaml
