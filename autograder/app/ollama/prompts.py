"""Versioned prompt templates.

Prompts live in ``prompts/*.txt`` with YAML front-matter::

    ---
    version: "1.0"
    task: test_generation
    ...
    ---

    <body>

The loader returns ``(metadata, body)``. ``prompt_hash`` (sha256 of the body,
16 hex chars) is recorded in every :class:`GenerationRecord` so a run is
auditable down to the exact prompt text used.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "prompts"


class PromptError(ValueError):
    """Raised when a prompt template is missing or malformed."""


def load_prompt(name: str) -> tuple[dict, str]:
    """Load ``prompts/<name>.txt``. Returns ``(front_matter, body)``."""
    path = PROMPTS_DIR / f"{name}.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptError(f"prompt template {name!r} not found: {exc}") from exc
    if not text.startswith("---\n"):
        raise PromptError(f"prompt template {name!r} is missing YAML front-matter")
    try:
        _, fm_text, body = text.split("---\n", 2)
    except ValueError as exc:
        raise PromptError(f"prompt template {name!r} has malformed front-matter") from exc
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a hard dep
        raise PromptError("PyYAML is required to read prompt front-matter") from exc
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise PromptError(f"prompt template {name!r}: bad front-matter YAML: {exc}") from exc
    if not isinstance(fm, dict) or "version" not in fm:
        raise PromptError(f"prompt template {name!r}: front-matter needs a 'version' key")
    return fm, body.strip() + "\n"


def prompt_hash(body: str) -> str:
    """Short fingerprint of the prompt body, for generation records."""
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
