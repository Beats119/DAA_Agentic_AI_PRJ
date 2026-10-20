"""Feedback generation + deterministic verification.

Two halves with a hard boundary between them:

1. **Generation** (LLM, via the gateway) drafts student feedback from
   *validated evidence only*. The model receives a JSON dump of evidence
   records — read-only data. It never sees the grade and cannot touch the
   scoring engine or the evidence registry.

2. **Verification** (deterministic, no LLM) checks the draft:
   - every cited evidence id exists in the finalized registry;
   - every claimed failure references a real recorded test result, and a
     test claimed as failing actually failed;
   - the grade is never restated or recomputed;
   - no contradiction with recorded analyzer outcomes (best-effort
     keyword check for the compile case);
   - the schema is valid (guaranteed by structured generation).

Failed drafts are regenerated within a bounded retry budget; if they keep
failing, a deterministic evidence-based fallback renders the feedback with
zero LLM involvement. The feedback path can never alter the grade or the
analyzer evidence: it only ever receives read-only copies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core.schemas import StrictModel
from ..ollama.models import model_cfg_for
from ..ollama.prompts import load_prompt

# Bounded regeneration budget for feedback drafts.
DEFAULT_FEEDBACK_RETRIES = 2

_EVIDENCE_ID_RE = re.compile(r"ev:[A-Za-z0-9_-]+:\d+")


class FeedbackFailure(StrictModel):
    evidence_id: str
    explanation: str
    suggestion: str


class FeedbackText(StrictModel):
    summary: str
    per_failure: list[FeedbackFailure] = []
    recommendations: list[str] = []
    uncertainty_notes: list[str] = []


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    violations: tuple[str, ...] = ()


def generate_feedback(
    evidence_json: str,
    gateway: Any,
    task: str = "feedback",
) -> tuple[FeedbackText, dict[str, Any]]:
    """Draft feedback from validated evidence (LLM). Never sees the grade."""
    fm, body = load_prompt("feedback")
    prompt = body.replace("{evidence_json}", evidence_json)
    model_cfg = model_cfg_for(task)
    feedback, record = gateway.generate_structured(
        task=task,
        prompt=prompt,
        schema=FeedbackText,
        model_cfg=model_cfg,
        prompt_name="feedback",
    )
    info = {
        "generation": record.model_dump(mode="json"),
        "prompt_version": fm.get("version", "unknown"),
        "model": model_cfg.get("model"),
    }
    return feedback, info


def _cited_evidence_ids(feedback: FeedbackText) -> set[str]:
    cited: set[str] = set()
    for failure in feedback.per_failure:
        cited.add(failure.evidence_id)
    texts = [feedback.summary]
    texts.extend(f.explanation for f in feedback.per_failure)
    texts.extend(f.suggestion for f in feedback.per_failure)
    texts.extend(feedback.recommendations)
    texts.extend(feedback.uncertainty_notes)
    for text in texts:
        cited.update(_EVIDENCE_ID_RE.findall(text or ""))
    return cited


def verify_feedback(
    feedback: FeedbackText,
    registry: Any,
    trusted_results: dict[str, dict[str, Any]],
    grade: Any | None,
    analyzer_statuses: dict[str, str] | None = None,
) -> VerificationResult:
    """Deterministically verify a feedback draft against recorded evidence.

    ``trusted_results`` maps test_id -> {passed, input, expected, actual,
    evidence_id}. ``grade`` is a read-only scalar used only for the
    no-restatement check.
    """
    violations: list[str] = []

    # 1. Every cited evidence id must exist in the finalized registry.
    for eid in sorted(_cited_evidence_ids(feedback)):
        try:
            registry.get(eid)
        except KeyError:
            violations.append(f"cites unknown evidence id {eid!r}")

    # 2. Claimed failures must match recorded test outcomes.
    for failure in feedback.per_failure:
        try:
            ev = registry.get(failure.evidence_id)
        except KeyError:
            continue  # already reported above
        test_id = (ev.data or {}).get("test_id") or (ev.data or {}).get("id")
        if test_id is None:
            # Evidence is not a test result; the claim is unverifiable as a
            # test failure — the explanation must stand on the evidence
            # itself, which exists (checked above). No violation.
            continue
        rec = trusted_results.get(str(test_id))
        if rec is None:
            violations.append(
                f"claims failure for test {test_id!r} which has no recorded "
                "trusted-test result"
            )
        elif rec.get("passed", True):
            violations.append(
                f"claims failure for test {test_id!r} which is recorded as "
                "passed — contradicts the evidence"
            )

    # 3. The grade must never be restated or recomputed. Only flag the exact
    # grade value appearing in a grade/score context (or a /100 fraction) —
    # deliberately narrow to avoid false positives on ordinary numbers.
    if grade is not None:
        gstr = str(grade)
        grade_pat = re.compile(
            r"(?i)\b(grade|score|marks|points)\b[^.\n]{0,60}"
            + re.escape(gstr)
            + r"|\b"
            + re.escape(gstr)
            + r"\s*/\s*100\b"
        )
        for label, text in (
            ("summary", feedback.summary),
            *[(f"per_failure[{i}]", f.explanation) for i, f in enumerate(feedback.per_failure)],
            *[(f"recommendation[{i}]", r) for i, r in enumerate(feedback.recommendations)],
        ):
            if text and grade_pat.search(text):
                violations.append(
                    f"{label} appears to restate the grade "
                    "(feedback must never state the grade)"
                )

    # 4. Best-effort contradiction check: claims of compile failure must not
    # contradict a recorded clean compile.
    if analyzer_statuses and analyzer_statuses.get("compiler") == "completed":
        compile_fail_pat = re.compile(
            r"(?i)\b(did not compile|failed to compile|compilation (failed|error))"
        )
        for label, text in (
            ("summary", feedback.summary),
            *[(f"per_failure[{i}]", f.explanation) for i, f in enumerate(feedback.per_failure)],
        ):
            if text and compile_fail_pat.search(text):
                violations.append(
                    f"{label} claims a compile failure but the compiler "
                    "analyzer recorded a successful compilation"
                )

    return VerificationResult(ok=not violations, violations=tuple(violations))


def deterministic_fallback(
    *,
    failing_tests: list[dict[str, Any]],
    analyzer_notes: list[str],
    uncertainty_notes: list[str],
    test_stats: dict[str, int],
    run_status: str,
) -> FeedbackText:
    """Render feedback purely from evidence. Zero LLM involvement.

    Used when the LLM gateway is unavailable or every draft fails
    verification. Never states the grade.
    """
    n_fail = len(failing_tests)
    n_gen = test_stats.get("generated", 0)
    n_trusted = test_stats.get("trusted", 0)

    if n_fail == 0 and n_trusted > 0:
        summary = (
            "All trusted correctness tests passed. "
            f"{n_trusted} of {n_gen} generated tests had a trustworthy oracle "
            "and contributed to the confirmed score."
        )
    elif n_trusted == 0:
        summary = (
            "No test achieved a trustworthy oracle, so correctness could not "
            "be confirmed from test evidence. The evaluation completed on "
            "the remaining analyzers; see the uncertainty notes."
        )
    else:
        summary = (
            f"{n_fail} of {n_trusted} trusted correctness tests failed. "
            "Each failure below cites the exact input, the trusted expected "
            "output, and what the program produced."
        )
    summary += f" Run status: {run_status}."

    per_failure: list[FeedbackFailure] = []
    for t in failing_tests:
        provenance = t.get("oracle_provenance") or {}
        tier = provenance.get("tier", "?")
        method = provenance.get("method", "?")
        per_failure.append(
            FeedbackFailure(
                evidence_id=t.get("evidence_id", ""),
                explanation=(
                    f"Test {t.get('test_id')} failed: input {t.get('input')!r}, "
                    f"trusted expected output {t.get('expected')!r} "
                    f"(oracle: tier {tier}, {method}), actual output "
                    f"{t.get('actual')!r}."
                ),
                suggestion=(
                    "Trace the failing input through the code by hand and "
                    "compare each intermediate value against the expected "
                    "output; fix the first divergence you find."
                ),
            )
        )

    recommendations = list(analyzer_notes)
    return FeedbackText(
        summary=summary,
        per_failure=per_failure,
        recommendations=recommendations,
        uncertainty_notes=list(uncertainty_notes),
    )


def finalize_feedback(
    *,
    evidence_json: str,
    gateway: Any | None,
    registry: Any,
    trusted_results: dict[str, dict[str, Any]],
    grade: Any | None,
    analyzer_statuses: dict[str, str] | None = None,
    failing_tests: list[dict[str, Any]] | None = None,
    analyzer_notes: list[str] | None = None,
    uncertainty_notes: list[str] | None = None,
    test_stats: dict[str, int] | None = None,
    run_status: str = "completed",
    max_retries: int = DEFAULT_FEEDBACK_RETRIES,
) -> dict[str, Any]:
    """Generate → verify → bounded retry → deterministic fallback.

    Returns ``{"feedback", "attempts", "used_fallback", "violations",
    "generation_records"}``. The grade and evidence are only ever read.
    """
    generation_records: list[dict[str, Any]] = []
    attempts = 0
    violations: tuple[str, ...] = ()

    def _fallback(reason: str) -> dict[str, Any]:
        notes = list(uncertainty_notes or [])
        notes.append(f"LLM feedback unavailable: {reason}; deterministic fallback used.")
        feedback = deterministic_fallback(
            failing_tests=failing_tests or [],
            analyzer_notes=analyzer_notes or [],
            uncertainty_notes=notes,
            test_stats=test_stats or {},
            run_status=run_status,
        )
        return {
            "feedback": feedback,
            "attempts": attempts,
            "used_fallback": True,
            "violations": violations,
            "generation_records": generation_records,
        }

    if gateway is None:
        return _fallback("no LLM gateway for this run")

    last_feedback: FeedbackText | None = None
    prompt_note = ""
    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        try:
            payload = evidence_json + prompt_note
            feedback, info = generate_feedback(payload, gateway)
        except Exception as exc:  # noqa: BLE001 - gateway faults -> retry/fallback
            violations = (f"generation attempt {attempts} raised {type(exc).__name__}: {exc}",)
            prompt_note = ""
            continue
        generation_records.append(info)
        last_feedback = feedback
        result = verify_feedback(
            feedback, registry, trusted_results, grade, analyzer_statuses
        )
        violations = result.violations
        if result.ok:
            return {
                "feedback": feedback,
                "attempts": attempts,
                "used_fallback": False,
                "violations": (),
                "generation_records": generation_records,
            }
        # Bounded retry: tell the model exactly what was rejected.
        prompt_note = (
            "\n\nYour previous draft was REJECTED for these reasons; "
            "fix them and cite only evidence IDs from the list above:\n- "
            + "\n- ".join(violations)
        )

    return _fallback(
        f"{attempts} draft(s) failed verification: {'; '.join(violations)}"
    )


def build_trusted_results(
    registry: Any,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Reconstruct trusted test outcomes from finalized correctness evidence.

    Returns ``(trusted_results, failing_tests)`` where ``trusted_results``
    maps test_id -> {passed, input, expected, actual, evidence_id,
    oracle_provenance} and ``failing_tests`` is the failing subset.
    """
    trusted: dict[str, dict[str, Any]] = {}
    for ev in registry.all():
        if ev.analyzer != "correctness" or ev.kind != "test_result":
            continue
        data = ev.data or {}
        test_id = str(data.get("id") or data.get("test_id") or ev.id)
        rec = {
            "test_id": test_id,
            "passed": bool(data.get("passed", False)),
            "input": data.get("input"),
            "expected": data.get("expected"),
            "actual": data.get("actual"),
            "evidence_id": ev.id,
            "oracle_provenance": data.get("oracle_provenance") or {},
        }
        trusted[test_id] = rec
    failing = [r for r in trusted.values() if not r["passed"]]
    return trusted, failing
