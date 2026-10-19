"""Real-Ollama integration tests.

These hit the ACTUAL local Ollama at http://127.0.0.1:11434. They SKIP with
an explicit reason when Ollama is unreachable or the model is missing —
never fake a pass. Run on the target machine (Khushi's) where Ollama +
llama3.1 are installed.
"""

from __future__ import annotations

import pytest

from autograder.app.ollama.client import OllamaClient, OllamaError
from autograder.app.ollama.health import ensure_model, probe

REAL_URL = "http://127.0.0.1:11434"


def _real_status():
    return probe(REAL_URL)


def test_real_ollama_reachable():
    status = _real_status()
    if not status["reachable"]:
        pytest.skip(f"Ollama not reachable: {status['error']}")
    assert status["models"], "Ollama reachable but no models installed"


def test_real_tags_roundtrip():
    status = _real_status()
    if not status["reachable"]:
        pytest.skip("Ollama not reachable")
    client = OllamaClient(REAL_URL)
    try:
        models = client.tags()
    finally:
        client.close()
    assert isinstance(models, list) and models


def test_real_model_for_test_generation():
    """The configured test-generation model must actually be installed."""
    try:
        result = ensure_model("test_generation", REAL_URL)
    except OllamaError as exc:
        pytest.skip(reason=str(exc))
    assert result["ok"]


def test_real_structured_generation():
    """End-to-end structured generation against the real local model."""
    from pydantic import BaseModel, ConfigDict

    from autograder.app.ollama.models import model_cfg_for

    status = _real_status()
    if not status["reachable"]:
        pytest.skip("Ollama not reachable")

    class Echo(BaseModel):
        model_config = ConfigDict(extra="forbid")
        answer: str

    try:
        cfg = model_cfg_for("test_generation")
        ensure_model("test_generation", REAL_URL)
    except OllamaError as exc:
        pytest.skip(reason=str(exc))
    client = OllamaClient(REAL_URL)
    try:
        item, record = client.generate_structured(
            task="smoke",
            prompt='Reply with strict JSON and nothing else: {"answer": "ok"}',
            schema=Echo,
            model_cfg=cfg,
        )
    finally:
        client.close()
    assert item.answer.strip().lower().startswith("ok")
    assert record.model == cfg["model"]
