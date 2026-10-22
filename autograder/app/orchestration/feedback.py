"""Feedback generation + deterministic verification.

Two halves with a hard boundary between them:

1. **Generation** (LLM, via the gateway) drafts student feedback from
   *validated evidence only*. The model receives a JSON dump of the
   citable subset of the evidence registry (confirmed failures, analyzer
   findings, metric summaries, limitations — see
   :func:`build_feedback_prompt_payload`) — read-only data. It never
   sees the grade and cannot touch the scoring engine or the evidence
   registry.

2. **Verification** (deterministic, no LLM) checks the draft:
   - every cited evidence id exists in the finalized registry;
   - every claimed failure references a real recorded test result, and a
     test claimed as failing actually failed;
   - the grade is never restated or recomputed;
   - no contradiction with recorded analyzer outcomes (best-effort
     keyword check for the compile case);
   - every cited source location (``line 42``, ``lines 10-15``,
     ``file.c:33``, ``:33``) names a line that actually exists in the
     submission — checked only when the caller supplies the source line
     count (the workflow always does; direct callers that omit it skip
     this check, since a line count cannot be guessed honestly);
   - every metric-score claim names a metric that exists in the run's
     metric set and states its recorded score (on either the 0-1 or the
     0-100 scale) — checked only when the caller supplies the per-metric
     scores;
   - the schema is valid (guaranteed by structured generation).

3. **Supplementary LLM audit** (OPTIONAL, off by default): when enabled
   via ``finalize_feedback(..., llm_check=True)`` — the workflow resolves
   it from ``evaluate(feedback_llm_check=...)`` or the
   ``AUTOGRADER_FEEDBACK_LLM_CHECK`` env var — a draft that PASSED the
   deterministic verifier is additionally audited by the local model
   against the evidence registry (``prompts/feedback_check.txt``). Its
   objections are treated exactly like deterministic violations: they
   trigger the same bounded regeneration, nothing more. The audit can
   never edit a draft, the evidence, or the grade, and if the audit
   itself fails (model down, bad output) the deterministically verified
   draft stands — the deterministic verifier is authoritative.

Failed drafts are regenerated within a bounded retry budget; if they keep
failing, a deterministic evidence-based fallback renders the feedback with
zero LLM involvement. The feedback path can never alter the grade or the
analyzer evidence: it only ever receives read-only copies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from ..core.schemas import StrictModel
from ..ollama.client import OllamaModelMissing, OllamaUnreachable
from ..ollama.models import model_cfg_for
from ..ollama.prompts import load_prompt

# Bounded regeneration budget for feedback drafts.
DEFAULT_FEEDBACK_RETRIES = 2

_EVIDENCE_ID_RE = re.compile(r"ev:[A-Za-z0-9_-]+:\d+")

# --- source-location citations (D21) -------------------------------------
# Feedback prose cites source locations in a handful of conventional
# shapes: "line 42", "lines 10-15" / "lines 10 to 15", "add.c:33",
# "src/main.c:10-12", and the bare ":33". Each pattern captures the line
# number(s); ranges capture both endpoints.
_LINE_CITATION_RES = (
    re.compile(r"(?i)\blines?\s+(\d+)(?:\s*(?:-|\bto\b)\s*(\d+))?"),
    re.compile(r"(?i)\b[\w.\-/]+\.(?:c|h)\s*:\s*(\d+)(?:\s*-\s*(\d+))?"),
    # Bare ":33" — the colon must not be preceded by a word char, dot or
    # another colon, so times ("12:30"), ratios ("1:3") and key:value
    # pairs ("expected:5") do not match.
    re.compile(r"(?<![\w:.]):(\d+)\b"),
)


def _cited_line_numbers(text: str | None) -> set[int]:
    """All line numbers cited in ``text`` (range endpoints included)."""
    found: set[int] = set()
    if not text:
        return found
    for pat in _LINE_CITATION_RES:
        for m in pat.finditer(text):
            for group in m.groups():
                if group is not None:
                    found.add(int(group))
    return found


# --- metric-score claims (D21) ---------------------------------------------
# A scoring claim about a KNOWN metric: the metric name, a score verb,
# then a number ("correctness scored 0.8", "score for compiler: 50").
# The number must match the recorded score on the 0-1 scale (tolerance
# covers 2-dp rounding) or the 0-100 scale (tolerance covers rounding
# to the nearest point).
_SCORE_TOL_FRAC = 0.011
_SCORE_TOL_PCT = 0.6

# Claims naming a metric that might not exist: only unambiguous shapes
# are flagged — "metric X scored …" (any name explicitly introduced as
# a metric) and snake_case tokens, which is how this system's metrics
# are named ("static_analysis scored …", "score for memory_safety …").
# Ordinary prose ("the program scored 5", "score of the submission") is
# deliberately NOT treated as a metric claim.
_UNKNOWN_METRIC_CLAIM_RES = (
    re.compile(r"(?i)\bmetric\s+['\"]?([a-z][a-z0-9_]*)['\"]?\s+(?:scored|score\b)"),
    re.compile(r"(?i)\b([a-z][a-z0-9]*_[a-z0-9_]+)\s+(?:scored|scoring)\s+\d"),
    re.compile(r"(?i)\bscore\s+(?:for|of)\s+(?:the\s+)?['\"]?([a-z][a-z0-9]*_[a-z0-9_]+)['\"]?"),
)


def _metric_score_claims(
    text: str | None, metric_scores: dict[str, float | None]
) -> list[tuple[str, float | None]]:
    """Extract ``(metric_name, claimed_value)`` scoring claims from text.

    ``claimed_value`` is ``None`` when a claim names an unknown metric
    without a parseable value — the caller reports it as unknown either
    way. Claims about known metrics carry their stated number.
    """
    claims: list[tuple[str, float | None]] = []
    if not text:
        return claims
    lowered = text.lower()
    for name in sorted(metric_scores):
        pat = re.compile(
            r"(?i)\b" + re.escape(name) + r"\b[^.\n]{0,40}?"
            r"(?:scored|scoring|score)\b[^.\n]{0,12}?(\d+(?:\.\d+)?)"
            r"|\bscore\s+(?:for|of)\s+(?:the\s+)?" + re.escape(name) +
            r"\b[^.\n]{0,8}?(\d+(?:\.\d+)?)"
        )
        for m in pat.finditer(text):
            value = m.group(1) if m.group(1) is not None else m.group(2)
            claims.append((name, float(value)))
    for pat in _UNKNOWN_METRIC_CLAIM_RES:
        for m in pat.finditer(lowered):
            name = m.group(1)
            if name not in metric_scores:
                claims.append((name, None))
    return claims


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


class FeedbackCheckViolation(StrictModel):
    claim: str
    reason: str


class FeedbackCheckResult(StrictModel):
    """Output contract of ``prompts/feedback_check.txt``."""

    verdict: Literal["pass", "fail"]
    violations: list[FeedbackCheckViolation] = []


def check_feedback_llm(
    feedback: FeedbackText,
    evidence_json: str,
    gateway: Any,
) -> tuple[list[str], dict[str, Any]]:
    """Supplementary LLM grounding audit of a verified feedback draft.

    Runs ONLY after the deterministic verifier has passed, and only when
    explicitly enabled. Returns ``(violations, info)`` with violations in
    the same string shape the deterministic verifier produces, so an
    objection flows into the same bounded-regeneration path. This pass
    can only object — it never edits the draft, the evidence, or the
    grade. Raises on gateway/prompt/config failure; the caller treats
    that as "check unavailable", never as a violation.
    """
    fm, body = load_prompt("feedback_check")
    prompt = body.replace(
        "{feedback_json}", feedback.model_dump_json(indent=1)
    ).replace("{evidence_json}", evidence_json)
    model_cfg = model_cfg_for("feedback_check")
    result, record = gateway.generate_structured(
        task="feedback_check",
        prompt=prompt,
        schema=FeedbackCheckResult,
        model_cfg=model_cfg,
        prompt_name="feedback_check",
    )
    info = {
        "generation": record.model_dump(mode="json"),
        "prompt_version": fm.get("version", "unknown"),
        "model": model_cfg.get("model"),
        "task": "feedback_check",
    }
    if result.verdict != "fail":
        return [], info
    violations = [
        f"llm feedback check: claim {v.claim!r} is ungrounded: {v.reason}"
        for v in result.violations
    ]
    if not violations:
        violations = [
            "llm feedback check: verdict 'fail' with no itemized violations"
        ]
    return violations, info


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
    *,
    source_lines: int | None = None,
    metric_scores: dict[str, float | None] | None = None,
) -> VerificationResult:
    """Deterministically verify a feedback draft against recorded evidence.

    ``trusted_results`` maps test_id -> {passed, input, expected, actual,
    evidence_id}. ``grade`` is a read-only scalar used only for the
    no-restatement check.

    ``source_lines`` (keyword-only, D21): the number of lines in the
    submission source. When given, every source location cited in the
    draft must name an existing line; when ``None`` the location check
    is skipped (noted here because a verifier must not guess the count).
    ``metric_scores`` (keyword-only, D21): metric name -> recorded score
    (0-1 fraction, or ``None`` when the metric produced no score). When
    given, every metric-score claim in the draft must name a metric in
    this mapping and state its recorded score.
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

    # Prose fields scanned by checks 5 and 6: the summary, the per-failure
    # explanations AND suggestions, and the recommendations.
    prose_fields: list[tuple[str, str | None]] = [
        ("summary", feedback.summary),
        *[(f"per_failure[{i}].explanation", f.explanation)
          for i, f in enumerate(feedback.per_failure)],
        *[(f"per_failure[{i}].suggestion", f.suggestion)
          for i, f in enumerate(feedback.per_failure)],
        *[(f"recommendation[{i}]", r)
          for i, r in enumerate(feedback.recommendations)],
    ]

    # 5. Source-location claims (D21): every cited line must exist in the
    # submission. Skipped when the caller did not supply the line count.
    if source_lines is not None:
        for label, text in prose_fields:
            for n in sorted(_cited_line_numbers(text)):
                if n < 1 or n > source_lines:
                    violations.append(
                        f"{label} cites line {n} but the submission has "
                        f"{source_lines} lines"
                    )

    # 6. Metric-score claims (D21): a claimed metric score must name a
    # metric in the run's metric set and match its recorded score (on
    # either the 0-1 or the 0-100 scale). Skipped when the caller did
    # not supply the per-metric scores.
    if metric_scores is not None:
        for label, text in prose_fields:
            for name, claimed in _metric_score_claims(text, metric_scores):
                if name not in metric_scores:
                    violations.append(
                        f"{label} claims a score for unknown metric "
                        f"{name!r} (not in this run's metric set)"
                    )
                elif claimed is None:
                    violations.append(
                        f"{label} makes a scoring claim about metric "
                        f"{name!r} that cannot be verified"
                    )
                elif metric_scores[name] is None:
                    violations.append(
                        f"{label} claims metric {name!r} scored {claimed:g} "
                        "but that metric has no recorded score"
                    )
                else:
                    actual = metric_scores[name]  # type: ignore[assignment]
                    if not (
                        abs(claimed - actual) <= _SCORE_TOL_FRAC
                        or abs(claimed - 100.0 * actual) <= _SCORE_TOL_PCT
                    ):
                        violations.append(
                            f"{label} claims metric {name!r} scored "
                            f"{claimed:g} but the recorded score is "
                            f"{actual:g} (0-1 scale)"
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


# --- feedback prompt payload trimming (latency) ---------------------------
# The generate prompt used to embed ``registry.to_json()`` — the FULL
# evidence registry (~10.6k chars on a real run), re-read by the model on
# every draft attempt at slow local prompt-eval speeds. But a feedback
# draft may only discuss confirmed failures, metric results, and stated
# limitations, and :func:`verify_feedback` checks every claim against the
# registry OBJECT server-side — never against the prompt text. So the
# generate prompt now carries a citable subset:
#
# - ``test_result`` records are included ONLY for confirmed failures
#   (``data.passed is False`` or an id the caller lists as failing);
#   passing outcomes are summarized by count, never serialized per test.
# - Every other record is included — when in doubt an entry stays — but
#   bulky raw fields are stripped (see ``_PAYLOAD_DROP_KEYS``) and any
#   remaining long string is truncated (see the caps below). A ``tests_
#   generated`` record's quarantined-candidate list collapses to a count.
# - Context entries WITHOUT evidence ids are appended (metric summaries,
#   trusted-test outcome counts, the run's limitations). They carry no id,
#   so they can never be cited as evidence.
#
# The deterministic verifier and the opt-in LLM audit keep receiving the
# full registry / full ``evidence_json`` exactly as before — only the
# generate prompt's serialization changes.

# Raw blobs that duplicate parsed data in the same record; pure prompt bulk.
_PAYLOAD_DROP_KEYS = frozenset({
    "raw_stderr", "raw_stdout", "raw_output", "raw_tail", "source_code",
    "unified_diff", "diff",
})
# Generic cap for any string field inside a kept record.
_PAYLOAD_FIELD_CAP = 800
# A failing test's input/expected/actual are what the feedback explains,
# so they keep a larger (still bounded) budget.
_PAYLOAD_IO_FIELDS = frozenset({"input", "expected", "actual"})
_PAYLOAD_IO_CAP = 2000


def _truncate_for_payload(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"…[truncated {len(text) - cap} chars]"


def _strip_payload_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively strip/cap one payload value (see the rules above)."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in _PAYLOAD_DROP_KEYS:
                continue
            if k == "quarantined" and isinstance(v, list):
                # The feedback may state HOW MANY candidates were
                # quarantined, never their contents.
                out[k] = {"count": len(v)}
                continue
            out[k] = _strip_payload_value(v, key=k)
        return out
    if isinstance(value, list):
        return [_strip_payload_value(v) for v in value]
    if isinstance(value, str):
        cap = _PAYLOAD_IO_CAP if key in _PAYLOAD_IO_FIELDS else _PAYLOAD_FIELD_CAP
        return _truncate_for_payload(value, cap)
    return value


def build_feedback_prompt_payload(
    evidence_json: str,
    *,
    trusted_results: dict[str, dict[str, Any]] | None = None,
    failing_tests: list[dict[str, Any]] | None = None,
    metric_scores: dict[str, float | None] | None = None,
    analyzer_statuses: dict[str, str] | None = None,
    uncertainty_notes: list[str] | None = None,
) -> str:
    """Build the citable-subset payload for the feedback generate prompt.

    ``evidence_json`` is the full registry dump (``registry.to_json()``).
    Anything that is not a JSON list of record dicts is returned verbatim
    — trimming must never break an unfamiliar caller. Record dicts keep
    their exact shape (same keys/serialization as the full dump); only
    the subset and the field stripping above differ.
    """
    try:
        records = json.loads(evidence_json)
    except (TypeError, ValueError):
        return evidence_json
    if not isinstance(records, list) or not all(
        isinstance(r, dict) for r in records
    ):
        return evidence_json

    failing_ids: set[str] = set()
    for t in failing_tests or []:
        if t.get("evidence_id"):
            failing_ids.add(str(t["evidence_id"]))
    for rec in (trusted_results or {}).values():
        if not rec.get("passed", True) and rec.get("evidence_id"):
            failing_ids.add(str(rec["evidence_id"]))

    payload: list[Any] = []
    for rec in records:
        if rec.get("kind") == "test_result":
            data = rec.get("data") or {}
            confirmed_failure = (
                data.get("passed") is False or rec.get("id") in failing_ids
            )
            if not confirmed_failure:
                continue  # passing outcome: counted below, not serialized
        payload.append(_strip_payload_value(rec))

    if trusted_results:
        payload.append({
            "kind": "test_outcome_summary",
            "trusted_tests": len(trusted_results),
            "passed": sum(
                1 for r in trusted_results.values() if r.get("passed")),
            "failed": sum(
                1 for r in trusted_results.values() if not r.get("passed")),
        })
    if metric_scores is not None:
        payload.append({
            "kind": "metric_summary",
            "metrics": [
                {
                    "name": name,
                    "status": (analyzer_statuses or {}).get(name),
                    "score": score,
                }
                for name, score in metric_scores.items()
            ],
        })
    if uncertainty_notes:
        payload.append({
            "kind": "run_limitations",
            "limitations": list(uncertainty_notes),
        })
    return json.dumps(payload, indent=1)


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
    llm_check: bool = False,
    source_lines: int | None = None,
    metric_scores: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    """Generate → verify → bounded retry → deterministic fallback.

    Returns ``{"feedback", "attempts", "used_fallback", "violations",
    "generation_records", "llm_check"}``. The grade and evidence are only
    ever read.

    ``llm_check`` (default False) enables the supplementary LLM grounding
    audit (:func:`check_feedback_llm`) for drafts that pass the
    deterministic verifier. Its objections trigger the same bounded
    regeneration; an audit failure never rejects a verified draft.

    ``source_lines`` / ``metric_scores`` are forwarded to
    :func:`verify_feedback` for the D21 location and metric-score checks.
    """
    generation_records: list[dict[str, Any]] = []
    attempts = 0
    violations: tuple[str, ...] = ()
    llm_check_info: dict[str, Any] = {
        "enabled": bool(llm_check),
        "status": "disabled" if not llm_check else "not_run",
        "violations": [],
    }

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
            "llm_check": llm_check_info,
        }

    if gateway is None:
        return _fallback("no LLM gateway for this run")

    # The generate prompt carries the citable subset of the evidence
    # (see build_feedback_prompt_payload). Verification below and the
    # opt-in audit keep using the FULL registry / evidence_json.
    prompt_payload = build_feedback_prompt_payload(
        evidence_json,
        trusted_results=trusted_results,
        failing_tests=failing_tests,
        metric_scores=metric_scores,
        analyzer_statuses=analyzer_statuses,
        uncertainty_notes=uncertainty_notes,
    )

    prompt_note = ""
    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        try:
            payload = prompt_payload + prompt_note
            feedback, info = generate_feedback(payload, gateway)
        except (OllamaUnreachable, OllamaModelMissing) as exc:
            # The server is unreachable or the model is gone: no later
            # draft can succeed while that holds, so stop drafting now
            # instead of burning the remaining attempts against a dead
            # endpoint (this once cost ~18 minutes per run). The
            # deterministic fallback below reports what happened.
            violations = (f"generation attempt {attempts} raised {type(exc).__name__}: {exc}",)
            break
        except Exception as exc:  # noqa: BLE001 - gateway faults -> retry/fallback
            violations = (f"generation attempt {attempts} raised {type(exc).__name__}: {exc}",)
            prompt_note = ""
            continue
        generation_records.append(info)
        result = verify_feedback(
            feedback, registry, trusted_results, grade, analyzer_statuses,
            source_lines=source_lines, metric_scores=metric_scores,
        )
        violations = result.violations
        if result.ok and llm_check:
            # Supplementary LLM audit (opt-in). Objections are violations
            # like any other and trigger regeneration below; an audit
            # ERROR accepts the deterministically verified draft — the
            # deterministic verifier is authoritative.
            try:
                llm_violations, check_info = check_feedback_llm(
                    feedback, evidence_json, gateway
                )
            except Exception as exc:  # noqa: BLE001 - audit is supplementary
                llm_check_info["status"] = "unavailable"
                llm_check_info["detail"] = (
                    f"{type(exc).__name__}: {exc}"
                )[:300]
            else:
                generation_records.append(check_info)
                if llm_violations:
                    llm_check_info["status"] = "violations"
                    llm_check_info["violations"] = list(llm_violations)
                    violations = tuple(llm_violations)
                    result = VerificationResult(ok=False, violations=violations)
                else:
                    llm_check_info["status"] = "passed"
        if result.ok:
            return {
                "feedback": feedback,
                "attempts": attempts,
                "used_fallback": False,
                "violations": (),
                "generation_records": generation_records,
                "llm_check": llm_check_info,
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
        if data.get("outcome") == "infra_error":
            # D15 defense in depth: an infrastructure failure is not a
            # student test result, even if legacy evidence carries it
            # under the test_result kind.
            continue
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
