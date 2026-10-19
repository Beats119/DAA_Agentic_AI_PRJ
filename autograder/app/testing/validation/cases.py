"""Deterministic candidate validation (format, contract, duplicates).

Pure function of (candidate, spec): no LLM, no I/O, no randomness. A
candidate that fails here never reaches the oracle or the sandbox.
"""

from __future__ import annotations

import re

from ...core.schemas import Specification, TestCandidate, ValidationResult

_VALID_CATEGORIES = {
    "normal",
    "edge",
    "boundary",
    "tricky",
    "invalid",
    "discriminating",
}

_MAX_INPUT_BYTES = 100_000  # sanity cap; execution budget is enforced elsewhere


def validate_candidate(candidate: TestCandidate, spec: Specification) -> ValidationResult:
    """Check a candidate's format and basic contract conformance."""
    errors: list[str] = []

    if not candidate.id.strip():
        errors.append("empty test id")
    if not candidate.input:
        errors.append("empty input")
    elif len(candidate.input.encode("utf-8")) > _MAX_INPUT_BYTES:
        errors.append(
            f"input exceeds {_MAX_INPUT_BYTES} bytes "
            f"({len(candidate.input.encode('utf-8'))})"
        )
    if candidate.input_category not in _VALID_CATEGORIES:
        errors.append(
            f"unknown input_category {candidate.input_category!r}; "
            f"expected one of {sorted(_VALID_CATEGORIES)}"
        )
    # NOTE: an empty expected_value is legitimate (a correct program may print
    # nothing), so it is not an error.
    # requirement_refs must name real requirements (else coverage is fiction)
    known = {r.id for r in spec.requirements}
    for ref in candidate.requirement_refs:
        if ref not in known:
            errors.append(f"requirement_ref {ref!r} names no known requirement")

    contract_err = _check_input_contract(candidate.input, spec)
    if contract_err:
        errors.append(contract_err)

    return ValidationResult(ok=not errors, errors=errors)


def _check_input_contract(inp: str, spec: Specification) -> str | None:
    """Best-effort check of the input against the spec's stated contract.

    Only enforces what the spec actually states (numbers-only lines,
    declared ranges). When the spec is silent, any non-empty input passes —
    guessing a contract would reject valid tests.
    """
    texts = " ".join(r.text for r in spec.requirements if r.kind == "explicit").lower()
    if not texts:
        return None
    tokens = inp.split()
    if not tokens:
        return "input has no tokens"
    mentions_integers = any(
        kw in texts for kw in ("integer", "int ", "number", "n ", "two numbers")
    )
    if mentions_integers:
        bad = [t for t in tokens if not re.fullmatch(r"[+-]?\d+", t)]
        if bad and len(bad) == len(tokens):
            # All tokens non-numeric while the spec demands integers:
            # likely a format violation (whitespace-tricky cases still pass
            # because split() already normalized them).
            return (
                f"input tokens {bad[:3]!r} are not integers "
                "though the spec requires integer input"
            )
    return None
