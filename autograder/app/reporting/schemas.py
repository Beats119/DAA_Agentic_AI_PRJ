"""Report schemas and validation.

A report is valid only if:
1. It parses as :class:`~autograder.app.core.schemas.Report`.
2. Its totals reconcile: ``|sum(contributions) - grade| <= 0.05``.
3. Each per-metric contribution matches ``100 * weight * score`` (when the
   score is present) within float tolerance.
4. Its ``scoring_policy_version`` matches the engine's ``POLICY_VERSION``.

``validate_report`` raises :class:`ReportError` naming the first problem it
finds. Reports are produced by deterministic code from validated evidence —
never drafted freehand by an LLM.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..core.schemas import Report, ReportMetric, RunManifest
from ..scoring.engine import POLICY_VERSION

_TOTALS_TOL = 0.05
_CONTRIB_TOL = 1e-6


class ReportError(ValueError):
    """A report failed schema or consistency validation."""


def validate_report(data: Report | dict[str, Any]) -> Report:
    """Validate a report dict (or Report) and return the validated Report."""
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

    # Per-metric reconciliation applies only when a grade was computed.
    # For incomplete/failed runs (grade None) the scoring engine keeps the
    # measured per-metric scores as evidence but zeroes contributions (there
    # is no grade to contribute to); that is honest, not a mismatch.
    if report.grade is not None:
        for m in report.metrics:
            if m.score is not None:
                want = 100.0 * m.weight * m.score
                if abs(m.contribution - want) > _CONTRIB_TOL:
                    raise ReportError(
                        f"metric {m.name!r}: contribution {m.contribution} != "
                        f"100 * weight * score = {want}"
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
