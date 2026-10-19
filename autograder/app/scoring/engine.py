"""Deterministic scoring engine. Pure function, no LLM, no I/O.

``score(metric_results, rules) -> ScoreResult`` implements the entire grading
policy:

- ``grade = 100 * sum(w_i * s_i)`` over eligible metrics, computed with
  :class:`~decimal.Decimal` (string-constructed) and rounded half-up to one
  decimal place.
- A metric result is *eligible* only when its analyzer reports
  ``status == "completed"`` **and** a finite score in [0, 1]. Anything else
  (failed / skipped / unsupported / inconclusive / missing score) goes
  through the rules' missing-evidence policy — it is never silently scored.
- Required metric missing:
  - ``uncertainty_policy.required_metric_missing == "incomplete"``
    → status ``incomplete``, grade ``None``.
  - ``"provisional"`` → grade computed over the remaining metrics with
    renormalized weights, status ``completed_with_warnings``.
- Optional metric missing → ``uncertainty_policy.optional_metric_missing``:
  - ``renormalize``: drop it, rescale the remaining weights to sum to 1.
  - ``zero``: keep its weight, contribute 0.
  - ``exclude``: drop it entirely (remaining weights need not sum to 1).
  Every application is recorded in ``warnings``.
- Inconclusive correctness evidence never becomes a confirmed failure: an
  analyzer with ``status="inconclusive"`` is not eligible, so its would-be
  score cannot move the grade.

``POLICY_VERSION`` is recorded on every result and report; bump it whenever
this module's semantics change.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from ..core.numbers import quantize_tenth, to_decimal
from ..core.schemas import AnalyzerResult, RuleSet

POLICY_VERSION = "1.0"

ScoreStatus = Literal["complete", "completed_with_warnings", "incomplete"]


@dataclass(frozen=True)
class MetricScore:
    """Per-metric contribution. ``contribution`` is full precision (unrounded)
    so report totals reconcile: ``sum(contributions)`` rounds to ``grade``."""

    name: str
    analyzer: str
    weight: Decimal
    score: Decimal | None
    contribution: Decimal
    status: str
    note: str


@dataclass(frozen=True)
class ScoreResult:
    grade: Decimal | None
    per_metric: tuple[MetricScore, ...]
    status: ScoreStatus
    warnings: tuple[str, ...]
    policy_version: str = POLICY_VERSION


def _eligible(result: AnalyzerResult) -> bool:
    return (
        result.status == "completed"
        and result.score is not None
        and 0.0 <= result.score <= 1.0
    )


def score(metrics: list[AnalyzerResult], rules: RuleSet) -> ScoreResult:
    """Compute the grade from analyzer results under ``rules``.

    Raises :class:`ValueError` if two results name the same metric (that is a
    coordinator bug, not something to paper over).
    """
    by_name: dict[str, AnalyzerResult] = {}
    for m in metrics:
        if m.metric_name in by_name:
            raise ValueError(f"duplicate result for metric {m.metric_name!r}")
        by_name[m.metric_name] = m

    warnings: list[str] = []
    known = {mc.name for mc in rules.metrics}
    for name in sorted(set(by_name) - known):
        warnings.append(f"result for unknown metric {name!r} ignored (not in rules)")
    per_metric: list[MetricScore] = []
    excluded: set[str] = set()  # metric names dropped from the computation
    zeroed: dict[str, Decimal] = {}  # metric name -> original weight kept at 0
    renormalize_needed = False

    incomplete = False
    for mc in rules.metrics:
        res = by_name.get(mc.name)
        if res is not None and _eligible(res):
            continue  # handled in the aggregation pass below
        # --- missing evidence: apply the configured policy ---
        reason = (
            f"no result"
            if res is None
            else f"status={res.status}, score={res.score}"
        )
        if mc.required:
            mode = rules.uncertainty_policy.required_metric_missing
            if mode == "incomplete":
                incomplete = True
                warnings.append(
                    f"required metric {mc.name!r} unavailable ({reason}); "
                    "marking evaluation incomplete (grade None)"
                )
            else:  # provisional
                excluded.add(mc.name)
                renormalize_needed = True
                warnings.append(
                    f"required metric {mc.name!r} unavailable ({reason}); "
                    "provisional grade over remaining metrics (renormalized)"
                )
        else:
            mode = rules.uncertainty_policy.optional_metric_missing
            if mode == "renormalize":
                excluded.add(mc.name)
                renormalize_needed = True
                warnings.append(
                    f"optional metric {mc.name!r} unavailable ({reason}); "
                    "renormalizing remaining weights"
                )
            elif mode == "zero":
                zeroed[mc.name] = to_decimal(mc.weight)
                warnings.append(
                    f"optional metric {mc.name!r} unavailable ({reason}); "
                    "contributing 0 with weight kept"
                )
            else:  # exclude
                excluded.add(mc.name)
                warnings.append(
                    f"optional metric {mc.name!r} unavailable ({reason}); excluded"
                )

    if incomplete:
        for mc in rules.metrics:
            res = by_name.get(mc.name)
            per_metric.append(
                MetricScore(
                    name=mc.name,
                    analyzer=mc.analyzer,
                    weight=to_decimal(mc.weight),
                    score=to_decimal(res.score) if res and res.score is not None else None,
                    contribution=Decimal(0),
                    status=res.status if res else "missing",
                    note="evaluation incomplete: required evidence missing",
                )
            )
        return ScoreResult(
            grade=None,
            per_metric=tuple(per_metric),
            status="incomplete",
            warnings=tuple(warnings),
        )

    # --- aggregate over eligible metrics ---
    # Renormalize only when the policy says so (provisional required-missing
    # or optional "renormalize"). "exclude" deliberately leaves the remaining
    # weights untouched.
    eligible = [
        (mc, by_name[mc.name])
        for mc in rules.metrics
        if mc.name not in excluded and by_name.get(mc.name) is not None and _eligible(by_name[mc.name])
    ]
    total_w = sum(to_decimal(mc.weight) for mc, _ in eligible)
    if renormalize_needed and total_w > 0:
        scale = Decimal(1) / total_w
        warnings.append(
            "weights renormalized over "
            f"{len(eligible)}/{len(rules.metrics)} available metrics"
        )
    else:
        scale = Decimal(1)

    total = Decimal(0)
    for mc in rules.metrics:
        w = to_decimal(mc.weight)
        res = by_name.get(mc.name)
        if mc.name in excluded:
            per_metric.append(
                MetricScore(mc.name, mc.analyzer, w, None, Decimal(0),
                            res.status if res else "missing",
                            "excluded from scoring by missing-evidence policy")
            )
            continue
        if mc.name in zeroed:
            per_metric.append(
                MetricScore(mc.name, mc.analyzer, w, Decimal(0), Decimal(0),
                            res.status if res else "missing",
                            "unavailable: scored 0 with weight kept (policy=zero)")
            )
            continue
        # eligible: status == "completed" and score is not None (see _eligible)
        s = to_decimal(res.score)  # type: ignore[union-attr]
        w_eff = w * scale
        contrib = Decimal(100) * w_eff * s
        total += w_eff * s
        per_metric.append(
            MetricScore(mc.name, mc.analyzer, w, s, contrib, res.status, "")
        )

    # Surface every non-clean analyzer status, even when the metric was
    # excluded from scoring — the report must show what actually happened.
    for mc in rules.metrics:
        res = by_name.get(mc.name)
        if res is not None and res.status != "completed":
            warnings.append(f"metric {mc.name!r}: analyzer reported {res.status}")

    grade = quantize_tenth(Decimal(100) * total)
    status: ScoreStatus = "complete" if not warnings else "completed_with_warnings"
    return ScoreResult(
        grade=grade,
        per_metric=tuple(per_metric),
        status=status,
        warnings=tuple(warnings),
    )
