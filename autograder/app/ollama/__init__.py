"""Ollama model gateway (loopback only, no API keys)."""

from .client import (
    OllamaBadResponse,
    OllamaClient,
    OllamaError,
    OllamaModelMissing,
    OllamaUnreachable,
)
from .health import ensure_model, probe
from .models import ModelConfigError, load_models_config, model_cfg_for
from .prompts import PromptError, load_prompt, prompt_hash

__all__ = [
    "ModelConfigError",
    "OllamaBadResponse",
    "OllamaClient",
    "OllamaError",
    "OllamaModelMissing",
    "OllamaUnreachable",
    "PromptError",
    "ensure_model",
    "load_models_config",
    "load_prompt",
    "model_cfg_for",
    "probe",
    "prompt_hash",
]
