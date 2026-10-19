"""Specification extraction agent (explicit vs inferred vs ambiguous)."""

from .agent import SpecificationError, extract_specification, specification_to_json

__all__ = ["SpecificationError", "extract_specification", "specification_to_json"]
