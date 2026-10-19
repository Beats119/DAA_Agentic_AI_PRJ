"""Loading instructor rules from disk (JSON or YAML)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.schemas import RuleSet
from .validate import RulesError, validate_rules


def _parse(text: str, suffix: str) -> Any:
    if suffix == ".json":
        import json

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RulesError(f"rules file is not valid JSON: {exc}") from exc
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise RulesError(
                "PyYAML is not installed; cannot read YAML rules files"
            ) from exc
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RulesError(f"rules file is not valid YAML: {exc}") from exc
    raise RulesError(
        f"unsupported rules file extension {suffix!r}; use .json, .yaml or .yml"
    )


def load_rules(path: str | Path) -> RuleSet:
    """Read, parse and validate a rules file. Returns a :class:`RuleSet`.

    Raises :class:`RulesError` if the file is missing, unparsable, or invalid.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulesError(f"cannot read rules file {p}: {exc}") from exc
    return validate_rules(_parse(text, p.suffix.lower()))
