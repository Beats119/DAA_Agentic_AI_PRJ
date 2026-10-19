"""Tiered oracle validation subsystem."""

from .service import OracleService
from .strategies import (
    INSTRUCTOR,
    TIER1,
    TIER2,
    TIER3,
    LLMProposedOracle,
    OracleContext,
    OracleStrategy,
    ReferenceSolutionOracle,
    SpecExampleOracle,
    InstructorExpectedOracle,
    normalize_ws,
)

__all__ = [
    "INSTRUCTOR",
    "TIER1",
    "TIER2",
    "TIER3",
    "LLMProposedOracle",
    "OracleContext",
    "OracleStrategy",
    "OracleService",
    "ReferenceSolutionOracle",
    "SpecExampleOracle",
    "InstructorExpectedOracle",
    "normalize_ws",
]
