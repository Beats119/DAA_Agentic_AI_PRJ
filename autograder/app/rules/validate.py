"""Rule validation: the instructor's grading policy, checked hard.

Validation happens in two layers:
1. Pydantic structural validation (types, ranges, literals) — see
   :class:`~autograder.app.core.schemas.RuleSet`.
2. Semantic checks here: known analyzer ids, weight totals, metric sanity.

Invalid rules are *rejected* with a clear message. Weights that do not sum
to 1 are rejected unless the rules explicitly set ``normalize_weights: true``
— silent "fixing" of the instructor's policy is never acceptable.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..core.schemas import RuleSet

# Analyzer ids that may appear in RuleSet.metrics[].analyzer. The registry of
# implementations lives in Phase 5; this set is the validation source of truth
# for Phase 2 (a rule naming an unknown analyzer is rejected, never ignored).
ANALYZER_IDS: frozenset[str] = frozenset(
    {
        "correctness",
        "compiler",
        "static_analysis",
        "modularity",
        "complexity",
        "memory_safety",
        "readability",
        "similarity",
    }
)


class RulesError(ValueError):
    """Raised when a rules file is missing, malformed, or invalid."""


def _friendly(error: dict[str, Any]) -> str:
    loc = ".".join(str(p) for p in error.get("loc", ())) or "<root>"
    return f"{loc}: {error.get('msg')}"


def validate_rules(data: Any) -> RuleSet:
    """Validate raw parsed data and return a :class:`RuleSet`.

    Raises :class:`RulesError` with a human-readable message on any problem:
    missing fields, wrong types, unknown analyzer ids, negative limits,
    invalid thresholds, negative weights, weights not summing to 1 (within
    1e-9, unless ``normalize_weights: true``), duplicate metric names,
    bad literals.
    """
    if not isinstance(data, dict):
        raise RulesError(f"rules must be a mapping, got {type(data).__name__}")
    # Comment convention: top-level keys starting with "_" (e.g. "_comment")
    # are stripped before validation and do not affect the config hash.
    data = {k: v for k, v in data.items() if not str(k).startswith("_")}
    # Semantic check before Pydantic so the message names the real problem.
    metrics = data.get("metrics")
    if isinstance(metrics, list):
        for i, m in enumerate(metrics):
            if isinstance(m, dict):
                analyzer = m.get("analyzer")
                if analyzer is not None and analyzer not in ANALYZER_IDS:
                    raise RulesError(
                        f"metrics[{i}].analyzer: unknown analyzer {analyzer!r}; "
                        f"known: {sorted(ANALYZER_IDS)}"
                    )
    try:
        return RuleSet.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(_friendly(e) for e in exc.errors())
        raise RulesError(f"invalid rules: {details}") from exc
