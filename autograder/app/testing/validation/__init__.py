"""Deterministic test-case validation (no LLM)."""

from ...core.schemas import ValidationResult
from .cases import validate_candidate

__all__ = ["ValidationResult", "validate_candidate"]
