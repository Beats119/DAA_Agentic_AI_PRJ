"""Ollama availability probing and model assurance.

All functions degrade explicitly: nothing here ever pretends a model is
available when it is not. Callers (operations page, orchestrator preflight)
turn these results into actionable diagnostics.
"""

from __future__ import annotations

from .client import OllamaClient, OllamaUnreachable
from .models import ModelConfigError, load_models_config

# Heuristic: models likely to fit comfortably in 16 GB RAM alongside the
# toolchain. Used only for the *recommendation* list; availability is what
# matters, never the name.
_SMALL_MODEL_HINTS = ("llama3.1", "llama3.2", "qwen2.5", "mistral", "phi", "gemma2", "tinyllama")


def probe(base_url: str = "http://127.0.0.1:11434") -> dict:
    """Check the local Ollama server.

    Returns ``{"reachable": bool, "models": [...], "recommended": [...],
    "error": str|None}``. ``recommended`` is the subset of installed models
    matching small-model hints — a convenience, not a guarantee.
    """
    try:
        client = OllamaClient(base_url)
        models = client.tags()
        client.close()
    except OllamaUnreachable as exc:
        return {"reachable": False, "models": [], "recommended": [], "error": str(exc)}
    recommended = [
        m for m in models if any(h in m.lower() for h in _SMALL_MODEL_HINTS)
    ]
    return {"reachable": True, "models": models, "recommended": recommended, "error": None}


def ensure_model(task: str, base_url: str = "http://127.0.0.1:11434") -> dict:
    """Ensure the model configured for ``task`` is installed.

    Returns ``{"ok": True, "model": name}`` or raises :class:`OllamaError`
    with an actionable message naming the exact ``ollama pull`` command.
    """
    from .client import OllamaError

    try:
        cfgs = load_models_config()
    except ModelConfigError as exc:
        raise OllamaError(str(exc)) from exc
    if task not in cfgs:
        raise OllamaError(
            f"no model configured for task {task!r}; check configs/models.yaml"
        )
    model = str(cfgs[task]["model"])
    status = probe(base_url)
    if not status["reachable"]:
        raise OllamaError(
            f"Ollama is not reachable at {base_url} — cannot verify model "
            f"{model!r}. Start it with `ollama serve`, then ensure the model "
            f"with `ollama pull {model}`."
        )
    installed = status["models"]
    # Match on the base name too ("llama3.1" matches "llama3.1:latest").
    base = model.split(":")[0]
    if not any(m == model or m.split(":")[0] == base for m in installed):
        raise OllamaError(
            f"model {model!r} (needed for task {task!r}) is not installed. "
            f"Run: ollama pull {model}"
        )
    return {"ok": True, "model": model}
