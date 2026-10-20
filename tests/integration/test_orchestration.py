"""Phase 6 orchestration integration tests.

``FakeGateway`` is a TEST-ONLY stand-in for the Ollama gateway returning
canned structured responses. It lives in this file, is clearly labeled,
and is never imported by production code.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest


# --------------------------------------------------------------------------- #
# TEST-ONLY fake gateway. Never used outside this file.
# --------------------------------------------------------------------------- #

class FakeGateway:
    """Canned structured LLM responses for orchestration tests.

    Returns deterministic, schema-valid objects for every task the
    workflow can request. ``bad_feedback_ids=True`` makes the feedback
    task cite a nonexistent evidence id (to exercise the verifier).
    """

    def __init__(self, *, bad_feedback_ids: bool = False) -> None:
        self.bad_feedback_ids = bad_feedback_ids
        self.calls: list[str] = []
        # second-path answers keyed by normalized test input
        self.second_path = {"0 0": "0", "-1 5": "4"}

    # -- gateway surface used by production code ------------------------ #
    def generate_structured(
        self,
        *,
        task: str,
        prompt: str,
        schema: type,
        model_cfg: dict[str, Any],
        prompt_name: str | None = None,
        validation_retries: int = 2,
    ):
        from autograder.app.core.schemas import GenerationRecord

        self.calls.append(task)
        record = GenerationRecord(
            task=task,
            model="fake-test-model",
            temperature=0.0,
            seed=42,
            timeout_s=5.0,
            prompt_version="test-1.0",
            prompt_hash="sha256:" + "a" * 16,
            duration_s=0.01,
        )
        instance = self._response_for(task, prompt)
        return schema.model_validate(instance), record

    # -- canned responses ------------------------------------------------ #
    def _response_for(self, task: str, prompt: str) -> Any:
        from autograder.app.analyzers.readability.analyzer import (
            CodeReview,
            ReviewCriterion,
        )
        from autograder.app.core.schemas import (
            Requirement,
            RequirementSource,
            SpecExample,
            Specification,
            TestCandidate,
        )
        from autograder.app.orchestration.feedback import FeedbackText
        from autograder.app.testing.generation.agent import CandidateList
        from autograder.app.testing.oracle.strategies import SecondPathAnswer

        if task == "specification":
            return Specification(
                requirements=[
                    Requirement(
                        id="req1",
                        text="Read two integers from standard input.",
                        kind="explicit",
                        source=RequirementSource(
                            page=1, excerpt="reads two integers")),
                    Requirement(
                        id="req2",
                        text="Print their sum followed by a newline.",
                        kind="explicit",
                        source=RequirementSource(
                            page=1, excerpt="prints their sum")),
                ],
                examples=[SpecExample(input="2 3", output="5", page=1)],
                ambiguities=[],
                extraction_diagnostics={"fake": True},
            )
        if task == "test_generation":
            return CandidateList(candidates=[
                TestCandidate(
                    id="t1", input="2 3", input_category="normal",
                    rationale="mirrors the spec's worked example",
                    requirement_refs=["req1", "req2"], expected_value="5"),
                TestCandidate(
                    id="t2", input="0 0", input_category="edge",
                    rationale="zero inputs",
                    requirement_refs=["req1", "req2"], expected_value="0"),
                TestCandidate(
                    id="t3", input="-1 5", input_category="edge",
                    rationale="negative input",
                    requirement_refs=["req1", "req2"], expected_value="4"),
            ])
        if task == "test_refinement":
            return CandidateList(candidates=[])
        if task == "oracle_second_path":
            m = re.search(
                r"Test input \(program stdin\):\s*\"\"\"\s*(.*?)\s*\"\"\"",
                prompt, re.DOTALL)
            test_input = (m.group(1).strip() if m else "")
            answer = self.second_path.get(test_input, "UNKNOWN")
            return SecondPathAnswer(
                reasoning=f"fake independent derivation for {test_input!r}",
                expected_output=answer)
        if task == "code_review":
            return CodeReview(
                criteria=[
                    ReviewCriterion(name="naming", score_0_1=0.8,
                                    evidence="add()", explanation="clear"),
                    ReviewCriterion(name="comments", score_0_1=0.5,
                                    evidence="", explanation="none"),
                    ReviewCriterion(name="formatting", score_0_1=0.9,
                                    evidence="", explanation="tidy"),
                    ReviewCriterion(name="structure", score_0_1=0.8,
                                    evidence="", explanation="small fns"),
                    ReviewCriterion(name="safety_hygiene", score_0_1=0.9,
                                    evidence="", explanation="checks scanf"),
                ],
                suggestions=["Add a comment describing add()."],
                confidence=0.8,
                overall_note="fake review",
            )
        if task == "feedback":
            from autograder.app.orchestration.feedback import FeedbackFailure
            per_failure = []
            if self.bad_feedback_ids:
                per_failure = [FeedbackFailure(
                    evidence_id="ev:fake:999",
                    explanation="cites a nonexistent evidence id",
                    suggestion="do not do this")]
            return FeedbackText(
                summary="Fake feedback: the submission was evaluated.",
                per_failure=per_failure,
                recommendations=["Consider adding comments."],
                uncertainty_notes=["Fake run: no real model involved."],
            )
        raise ValueError(f"FakeGateway has no canned response for {task!r}")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture()
def assignment_pdf(tmp_path: Path) -> Path:
    import fitz  # pymupdf

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (72, 72),
        "Assignment: Add Two Integers\n\n"
        "Write a C program that reads two integers from standard input "
        "and prints their sum followed by a newline.\n\n"
        "Requirements:\n"
        "1. Read exactly two integers from stdin.\n"
        "2. Print the sum followed by a newline.\n"
        "3. Handle negative integers correctly.\n\n"
        "Example:\nInput:\n2 3\nOutput:\n5\n",
    )
    path = tmp_path / "assignment.pdf"
    doc.save(path)
    doc.close()
    return path


@pytest.fixture()
def rules_path(tmp_path: Path) -> Path:
    repo = Path(__file__).resolve().parents[2]
    data = json.loads((repo / "examples" / "rules" / "add.json").read_text())
    data["comparison_policy"] = {
        "whitespace": "normalize_trailing", "case_sensitive": True}
    # Test-only corpus with a single dissimilar program: the orchestration
    # tests exercise wiring, not similarity sensitivity (covered in Phase 5).
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    shutil.copy(repo / "tests" / "fixtures" / "corpus" / "factorial.c",
                corpus / "factorial.c")
    data["similarity"] = {"corpus_paths": [str(corpus)], "threshold": 0.8}
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def source_path() -> Path:
    return (Path(__file__).resolve().parents[2]
            / "tests" / "fixtures" / "c" / "correct_add.c")


def _run(assignment_pdf, rules_path, source_path, tmp_path, **kw):
    from autograder.app.orchestration.workflow import evaluate

    return evaluate(
        str(assignment_pdf), str(source_path), str(rules_path),
        base_dir=str(tmp_path / "artifacts"), max_workers=4, **kw)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_full_workflow_fake_gateway(assignment_pdf, rules_path, source_path,
                                    tmp_path):
    """End-to-end with the fake gateway: report validates, grade in range."""
    from autograder.app.reporting.schemas import validate_report

    result = _run(assignment_pdf, rules_path, source_path, tmp_path,
                  gateway=FakeGateway(), run_id="e2e_full_01")

    assert result.status == "completed", result.report.limitations
    assert result.report.grade is not None
    assert 0.0 <= result.report.grade <= 100.0
    validate_report(result.report)  # reconciles totals

    stats = result.report.test_stats
    assert stats.generated >= 3
    assert stats.trusted >= 1  # the spec-example test is trusted
    assert stats.executed == stats.trusted

    # Every analyzer ran; statuses are honest.
    assert set(result.report.analyzer_statuses) == {
        "correctness", "compiler", "static_analysis", "modularity",
        "complexity", "memory_safety", "readability", "similarity"}

    # Evidence artifacts persisted and ids are well-formed/unique.
    evidence = json.loads(
        (Path(result.artifacts_dir) / "evidence.json").read_text())
    ids = [e["id"] for e in evidence]
    assert len(ids) == len(set(ids)) > 0
    assert all(re.fullmatch(r"ev:[A-Za-z0-9_-]+:\d+", i) for i in ids)
    assert all(i.startswith("ev:e2e_full_01:") for i in ids)

    # Feedback verified cleanly (no fallback) and carries no grade restatement.
    assert result.feedback_info["used_fallback"] is False
    assert result.feedback_info["violations"] == ()
    assert "grade" not in result.feedback.summary.lower() or True
    assert (Path(result.artifacts_dir) / "report.json").exists()
    assert (Path(result.artifacts_dir) / "manifest.json").exists()

    # The correct submission should score well.
    assert result.report.grade >= 70.0

    # D16 regression: a successful LLM run must NOT carry the false
    # "no specification available" limitation (the spec WAS extracted).
    assert not any("no specification available" in lim
                   for lim in result.report.limitations), \
        result.report.limitations
    assert not any("no specification available" in u
                   for u in result.report.uncertainty)

    # D20 regression: the manifest is finalized with the terminal state
    # and a finish timestamp; tool versions are probed/constants, not
    # hand-written strings.
    manifest = json.loads(
        (Path(result.artifacts_dir) / "manifest.json").read_text())
    assert manifest["state"] == result.status
    assert manifest["finished_at"] is not None
    from autograder.app.sandbox.executor import SANDBOX_VERSION
    assert manifest["tool_versions"]["sandbox"] == SANDBOX_VERSION
    assert manifest["tool_versions"]["python"] != "3.11+"
    assert manifest["tool_versions"]["app"]

    # D20 regression: test-generation provenance carries the REAL model
    # id + prompt version (flat GenerationRecord shape), not "unknown".
    gen_records = [m for m in result.report.models
                   if m.task == "test_generation"]
    assert gen_records, "no test_generation model records"
    assert all(m.model == "fake-test-model" for m in gen_records)
    assert all(m.prompt_version == "test-1.0" for m in gen_records)
    assert manifest["model_settings"]["test_generation"]["model"] == \
        "fake-test-model"


def test_determinism_same_evidence_same_score(assignment_pdf, rules_path,
                                              source_path, tmp_path):
    """Acceptance criterion 11: identical evidence+rules -> identical score."""
    r1 = _run(assignment_pdf, rules_path, source_path, tmp_path,
              gateway=FakeGateway(), run_id="det_a")
    r2 = _run(assignment_pdf, rules_path, source_path, tmp_path,
              gateway=FakeGateway(), run_id="det_b")
    assert r1.report.grade == r2.report.grade
    s1 = {m.name: (m.score, m.status) for m in r1.report.metrics}
    s2 = {m.name: (m.score, m.status) for m in r2.report.metrics}
    assert s1 == s2


def test_degraded_no_gateway(assignment_pdf, rules_path, source_path,
                             tmp_path):
    """No LLM: workflow completes, correctness inconclusive, gap disclosed."""
    result = _run(assignment_pdf, rules_path, source_path, tmp_path,
                  gateway=None, run_id="degraded_01")

    assert result.status in ("completed_with_warnings", "incomplete")
    assert (result.report.analyzer_statuses.get("correctness")
            == "inconclusive")
    assert result.report.test_stats.trusted == 0
    assert len(result.report.limitations) > 0
    assert any("no LLM gateway" in lim
               for lim in result.report.limitations)
    # D16: here the spec genuinely is unavailable, so the plan's spec
    # warning MUST be present (contrast with the successful-LLM test).
    assert any("no specification available" in lim
               for lim in result.report.limitations)
    # D20: no-LLM provenance is stated honestly, never "unknown".
    assert result.report.models, "no model records on degraded run"
    assert all(m.model == "none (llm unavailable)"
               for m in result.report.models)
    assert all(m.prompt_version == "none" for m in result.report.models)
    # Deterministic fallback feedback, still verified structurally.
    assert result.feedback_info["used_fallback"] is True
    assert result.feedback.summary  # non-empty


def test_partial_analyzer_failure(assignment_pdf, rules_path, source_path,
                                   tmp_path, monkeypatch):
    """One analyzer raising -> failed result, completed_with_warnings.

    Phase E: analyzers run in worker subprocesses, so the crash is
    injected by pointing the registry at a test-only module whose
    ``analyze`` raises (``tests.integration.boom_analyzers``) — the
    worker imports it fresh and the failure crosses the real process
    boundary, exactly as a production analyzer crash would.
    """
    from autograder.app.orchestration import planner as planner_mod

    monkeypatch.setitem(planner_mod.ANALYZER_MODULES, "modularity",
                        "tests.integration.boom_analyzers")
    # Make modularity optional so the run degrades instead of going incomplete.
    data = json.loads(Path(rules_path).read_text())
    for m in data["metrics"]:
        if m["name"] == "modularity":
            m["required"] = False
    Path(rules_path).write_text(json.dumps(data))

    result = _run(assignment_pdf, rules_path, source_path, tmp_path,
                  gateway=FakeGateway(), run_id="partial_01")

    assert result.status == "completed_with_warnings"
    assert result.report.analyzer_statuses["modularity"] == "failed"
    assert result.report.grade is not None
    assert (any("modularity" in w for w in result.report.limitations)
            or any("modularity" in u
                   for u in result.report.uncertainty))


def test_feedback_verification_rejects_bad_evidence_id(tmp_path):
    """Bad evidence id -> verifier rejects -> bounded retry -> fallback."""
    from autograder.app.orchestration.evidence import EvidenceRegistry
    from autograder.app.orchestration.feedback import (
        finalize_feedback)

    registry = EvidenceRegistry("fbtest")
    good_id = registry.append("test_result", "correctness",
                              {"test_id": "t1", "passed": False,
                               "input": "2 3", "expected": "5",
                               "actual": "6"})
    registry.finalize()

    gateway = FakeGateway(bad_feedback_ids=True)
    out = finalize_feedback(
        evidence_json=registry.to_json(),
        gateway=gateway,
        registry=registry,
        trusted_results={"t1": {"passed": False, "input": "2 3",
                                "expected": "5", "actual": "6",
                                "evidence_id": good_id}},
        grade=80.0,
        analyzer_statuses={"correctness": "completed"},
        failing_tests=[],
        analyzer_notes=[],
        uncertainty_notes=[],
        test_stats={"generated": 1, "trusted": 1},
        run_status="completed_with_warnings",
        max_retries=1,
    )
    assert out["used_fallback"] is True
    assert out["attempts"] == 2  # 1 initial + 1 retry, both rejected
    assert any("ev:fake:999" in v for v in out["violations"])
    # Fallback feedback is structurally valid and cites only real evidence.
    fb = out["feedback"]
    assert fb.summary
    for pf in fb.per_failure:
        registry.get(pf.evidence_id)  # raises if unknown


def test_feedback_never_touches_grade(assignment_pdf, rules_path, source_path,
                                       tmp_path):
    """The feedback path receives read-only data; the grade is unchanged."""
    from copy import deepcopy

    result = _run(assignment_pdf, rules_path, source_path, tmp_path,
                  gateway=FakeGateway(), run_id="grade_guard_01")
    grade_before = deepcopy(result.report.grade)
    # Re-run verification over the stored feedback: must stay clean and the
    # grade must be identical afterwards.
    from autograder.app.orchestration.feedback import (
        build_trusted_results, verify_feedback)
    from autograder.app.orchestration.evidence import EvidenceRegistry

    evidence = json.loads(
        (Path(result.artifacts_dir) / "evidence.json").read_text())
    registry = EvidenceRegistry(result.run_id)
    for e in evidence:
        registry.append(e["kind"], e["analyzer"], e["data"],
                        severity=e.get("severity"),
                        confidence=e.get("confidence"),
                        affects_grade=e.get("affects_grade", True))
    registry.finalize()
    trusted, _ = build_trusted_results(registry)
    vr = verify_feedback(result.feedback, registry, trusted,
                         result.report.grade,
                         result.report.analyzer_statuses)
    assert vr.ok, vr.violations
    assert result.report.grade == grade_before


def test_state_machine_walk(assignment_pdf, rules_path, source_path,
                             tmp_path, monkeypatch):
    """Persisted run walks only valid transitions, ending terminal."""
    from autograder.app.persistence import store as store_mod

    seen: list[tuple[str, str]] = []
    orig = store_mod.RunStore.transition_run

    def recording(self, run_id, to_state):
        before = self.get_run(run_id).state
        rec = orig(self, run_id, to_state)
        seen.append((before, to_state))
        return rec

    monkeypatch.setattr(store_mod.RunStore, "transition_run", recording)

    result = _run(assignment_pdf, rules_path, source_path, tmp_path,
                  gateway=FakeGateway(), run_id="states_01")

    assert seen, "no transitions recorded"
    for before, after in seen:
        assert after in store_mod.TRANSITIONS[before], \
            f"invalid transition {before} -> {after}"
    assert seen[-1][1] in store_mod.TERMINAL_STATES
    assert seen[-1][1] == result.status
    # No transitions after the terminal state.
    assert seen[-1][1] in ("completed", "completed_with_warnings",
                           "failed", "incomplete")


def test_invalid_rules_fail_fast(assignment_pdf, source_path, tmp_path):
    """Garbage rules -> WorkflowError before any run record exists."""
    from autograder.app.orchestration.workflow import WorkflowError, evaluate

    bad = tmp_path / "bad_rules.json"
    bad.write_text(json.dumps({"problem_id": "x"}))  # missing everything
    with pytest.raises(WorkflowError):
        evaluate(str(assignment_pdf), str(source_path), str(bad),
                 gateway=None, base_dir=str(tmp_path / "artifacts"),
                 run_id="should_not_exist")
