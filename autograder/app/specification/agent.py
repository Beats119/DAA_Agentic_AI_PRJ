"""Specification extraction agent: PDF text -> structured Specification.

Uses the versioned ``prompts/specification.txt`` through the Ollama gateway.
Post-validation is honest, not cosmetic:
- every ``explicit`` requirement must carry a non-empty source excerpt,
  else it is demoted to ``inferred`` with a diagnostic note;
- a run where the model marks *everything* ambiguous produces a warning
  (the document may be unparseable, not necessarily ambiguous);
- extraction_diagnostics always says what actually happened.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.schemas import AssignmentDoc, Requirement, Specification
from ..ollama.client import OllamaClient
from ..ollama.models import model_cfg_for
from ..ollama.prompts import load_prompt


class SpecificationError(RuntimeError):
    """Raised when a specification cannot be extracted or validated."""


def _render_assignment(doc: AssignmentDoc, max_chars: int = 12000) -> str:
    """Assignment text with per-page markers, bounded for the context window."""
    parts: list[str] = []
    budget = max_chars
    for page in doc.pages or []:
        chunk = f"[p{page.page_no}]\n{page.text.strip()}\n"
        if len(chunk) > budget:
            chunk = chunk[:budget]
        parts.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    text = "\n".join(parts).strip()
    return text if text else doc.text[:max_chars]


def extract_specification(
    doc: AssignmentDoc,
    client: OllamaClient,
    task: str = "specification",
    max_chars: int = 12000,
) -> tuple[Specification, dict[str, Any]]:
    """Extract a structured :class:`Specification` from an ingested document.

    Returns ``(specification, record)`` where ``record`` carries the
    :class:`GenerationRecord` plus post-validation diagnostics as plain data.
    Raises :class:`SpecificationError` on gateway/validation failure.
    """
    model_cfg = model_cfg_for(task)
    fm, body = load_prompt("specification")
    prompt = body.replace("{assignment_text}", _render_assignment(doc, max_chars))
    try:
        spec, gen_record = client.generate_structured(
            task=task,
            prompt=prompt,
            schema=Specification,
            model_cfg=model_cfg,
            prompt_name="specification",
        )
    except Exception as exc:
        raise SpecificationError(f"specification extraction failed: {exc}") from exc

    spec, notes = _post_validate(spec)
    record = {
        "generation": gen_record.model_dump(mode="json"),
        "prompt_version": fm.get("version", "unknown"),
        "post_validation_notes": notes,
    }
    return spec, record


def _post_validate(spec: Specification) -> tuple[Specification, list[str]]:
    """Honest post-checks. Demotes/warns, never invents."""
    notes: list[str] = []
    fixed: list[Requirement] = []
    for req in spec.requirements:
        if req.kind == "explicit" and not req.source.excerpt.strip():
            notes.append(
                f"requirement {req.id} claimed explicit without a source excerpt; "
                "demoted to inferred"
            )
            fixed.append(req.model_copy(update={"kind": "inferred"}))
        else:
            fixed.append(req)
    spec = spec.model_copy(update={"requirements": fixed})

    kinds = {r.kind for r in spec.requirements}
    if spec.requirements and kinds == {"ambiguous"}:
        notes.append(
            "model marked every requirement ambiguous — the document may be "
            "unparseable rather than genuinely ambiguous; treating spec as thin"
        )
    if not spec.requirements and not spec.examples:
        notes.append("no requirements and no examples extracted; spec is thin")

    diag = dict(spec.extraction_diagnostics or {})
    diag["post_validation_notes"] = notes
    diag["counts"] = {
        "explicit": sum(1 for r in spec.requirements if r.kind == "explicit"),
        "inferred": sum(1 for r in spec.requirements if r.kind == "inferred"),
        "ambiguous": sum(1 for r in spec.requirements if r.kind == "ambiguous"),
        "examples": len(spec.examples),
        "ambiguities": len(spec.ambiguities),
    }
    spec = spec.model_copy(update={"extraction_diagnostics": diag})
    return spec, notes


def specification_to_json(spec: Specification) -> str:
    """Pretty JSON for prompt embedding / debugging."""
    return json.dumps(spec.model_dump(mode="json"), indent=1)
