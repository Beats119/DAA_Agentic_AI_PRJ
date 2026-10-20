"""Report schemas and validation.

A report is valid only if:
1. It parses as :class:`~autograder.app.core.schemas.Report`.
2. Its totals reconcile: ``|sum(contributions) - grade| <= 0.05``.
3. Each per-metric contribution matches ``100 * effective_weight * score``
   (when the score is present) within float tolerance. The **effective**
   weight is authoritative (policy 1.1); the legacy ``weight`` field must
   agree with it when both are present.
4. Its ``scoring_policy_version`` matches the engine's ``POLICY_VERSION``.
5. When the :class:`RuleSet` is supplied, each metric's
   ``configured_weight`` must match the rules file (and be present), and
   every reported metric must exist in the rules.

``validate_report`` raises :class:`ReportError` naming the first problem it
finds. Reports are produced by deterministic code from validated evidence —
never drafted freehand by an LLM.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..core.schemas import Report, ReportMetric, RuleSet, RunManifest
from ..scoring.engine import POLICY_VERSION

_TOTALS_TOL = 0.05
_CONTRIB_TOL = 1e-6
_WEIGHT_TOL = 1e-9


class ReportError(ValueError):
    """A report failed schema or consistency validation."""


def _effective_weight(m: ReportMetric) -> float:
    """The weight reconciliation must use (effective, policy 1.1)."""
    return m.effective_weight if m.effective_weight is not None else m.weight


def validate_report(
    data: Report | dict[str, Any], rules: RuleSet | None = None
) -> Report:
    """Validate a report dict (or Report) and return the validated Report.

    When ``rules`` is given, additionally verify that every metric's
    ``configured_weight`` matches the rules file — the report must not
    quietly grade against different weights than the instructor configured.
    """
    if isinstance(data, dict):
        try:
            report = Report.model_validate(data)
        except ValidationError as exc:
            raise ReportError(f"report schema invalid: {exc.errors()}") from exc
    elif isinstance(data, Report):
        report = data
    else:
        raise ReportError(f"expected Report or dict, got {type(data).__name__}")

    if report.scoring_policy_version != POLICY_VERSION:
        raise ReportError(
            f"scoring_policy_version {report.scoring_policy_version!r} != "
            f"engine POLICY_VERSION {POLICY_VERSION!r}"
        )

    # Dual-weight consistency (policy 1.1): the legacy ``weight`` field is
    # defined as the effective weight; the two must not drift apart.
    for m in report.metrics:
        if m.effective_weight is not None and abs(m.weight - m.effective_weight) > _CONTRIB_TOL:
            raise ReportError(
                f"metric {m.name!r}: weight {m.weight} disagrees with "
                f"effective_weight {m.effective_weight}"
            )

    if rules is not None:
        configured = {mc.name: float(mc.weight) for mc in rules.metrics}
        for m in report.metrics:
            if m.name not in configured:
                raise ReportError(
                    f"metric {m.name!r} is not present in the rules"
                )
            if m.configured_weight is None:
                raise ReportError(
                    f"metric {m.name!r}: configured_weight missing "
                    "(required when validating against rules)"
                )
            if abs(m.configured_weight - configured[m.name]) > _WEIGHT_TOL:
                raise ReportError(
                    f"metric {m.name!r}: configured_weight "
                    f"{m.configured_weight} != rules weight "
                    f"{configured[m.name]}"
                )

    # Per-metric reconciliation applies only when a grade was computed.
    # For incomplete/failed runs (grade None) the scoring engine keeps the
    # measured per-metric scores as evidence but zeroes contributions (there
    # is no grade to contribute to); that is honest, not a mismatch.
    if report.grade is not None:
        for m in report.metrics:
            if m.score is not None:
                want = 100.0 * _effective_weight(m) * m.score
                if abs(m.contribution - want) > _CONTRIB_TOL:
                    raise ReportError(
                        f"metric {m.name!r}: contribution {m.contribution} != "
                        f"100 * effective_weight * score = {want}"
                    )

    if report.grade is not None:
        total = sum(m.contribution for m in report.metrics)
        if abs(total - report.grade) > _TOTALS_TOL:
            raise ReportError(
                f"totals do not reconcile: sum(contributions)={total:.4f} "
                f"vs grade={report.grade} (tolerance {_TOTALS_TOL})"
            )
    return report


__all__ = ["Report", "ReportMetric", "RunManifest", "ReportError", "validate_report"]
