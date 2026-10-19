"""Exact decimal helpers shared by scoring and report validation.

All grade arithmetic uses :class:`~decimal.Decimal` built from the *string*
form of inputs (never from binary floats) and rounds half-up. This keeps
``0.1 + 0.2``-style artifacts out of grades entirely.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

_ONE_TENTH = Decimal("0.1")


def to_decimal(value: int | float | str | Decimal) -> Decimal:
    """Convert a numeric value to Decimal via its string form."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_tenth(value: Decimal) -> Decimal:
    """Round a Decimal to one decimal place, half-up."""
    return value.quantize(_ONE_TENTH, rounding=ROUND_HALF_UP)


def round_half_up_1(value: float) -> float:
    """Round a float to one decimal place, half-up (for report display)."""
    return float(quantize_tenth(to_decimal(value)))
