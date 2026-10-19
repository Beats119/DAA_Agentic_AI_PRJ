"""Integration tests for the Ollama gateway — against a CLEARLY LABELED
test-only mock server (see conftest.py). No production path is involved."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict

from autograder.app.ollama.client import (
    OllamaBadResponse,
    OllamaClient,
    OllamaError,
    OllamaModelMissing,
    OllamaUnreachable,
)
from autograder.app.ollama.health import ensure_model, probe


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    score: float


def _cfg(**over):
    cfg = {"model": "llama3.1", "temperature": 0.0, "seed": 42, "timeout_s": 5.0}
    cfg.update(over)
    return cfg


def test_tags_listing(mock_ollama):
    url, server = mock_ollama
    server.script["tags"] = [("json", {"models": [{"name": "llama3.1:latest"}]})]
    client = OllamaClient(url)
    assert client.tags() == ["llama3.1:latest"]
    client.close()


def test_generate_success_and_record(mock_ollama):
    url, server = mock_ollama
    server.script["generate"] = [
        ("json", {"response": '{"name": "x", "score": 0.5}'})
    ]
    client = OllamaClient(url)
    item, record = client.generate_structured(
        task="t", prompt="p", schema=_Item, model_cfg=_cfg(), prompt_name="specification"
    )
    assert item.name == "x" and item.score == 0.5
    assert record.task == "t" and record.model == "llama3.1"
    assert record.temperature == 0.0 and record.seed == 42
    assert record.prompt_version == "1.0"  # from prompts/specification.txt
    assert record.prompt_hash.startswith("sha256:")
    # No Authorization header was sent.
    assert b"authorization" not in server.last_request_body.lower()
    client.close()


def test_retry_on_500_then_success(mock_ollama):
    url, server = mock_ollama
    server.script["generate"] = [
        ("status", 500),
        ("json", {"response": '{"name": "x", "score": 0.5}'}),
    ]
    client = OllamaClient(url, max_retries=2, backoff_s=0.01)
    text = client.generate("hi", model="llama3.1")
    assert json.loads(text) == {"name": "x", "score": 0.5}
    client.close()


def test_malformed_then_validation_retry_success(mock_ollama):
    url, server = mock_ollama
    server.script["generate"] = [
        ("raw", "not json at all"),
        ("json", {"response": '{"name": "x", "score": "high"}'}),  # bad type
        ("json", {"response": '{"name": "x", "score": 0.9}'}),
    ]
    client = OllamaClient(url, backoff_s=0.01)
    item, _ = client.generate_structured(
        task="t", prompt="p", schema=_Item, model_cfg=_cfg(), validation_retries=2
    )
    assert item.score == 0.9
    client.close()


def test_persistent_garbage_raises(mock_ollama):
    url, server = mock_ollama
    server.script["generate"] = [("raw", "garbage")] * 5
    client = OllamaClient(url, backoff_s=0.01)
    with pytest.raises(OllamaBadResponse):
        client.generate_structured(
            task="t", prompt="p", schema=_Item, model_cfg=_cfg(), validation_retries=1
        )
    client.close()


def test_model_missing_names_pull_command(mock_ollama):
    url, server = mock_ollama
    server.script["generate"] = [
        ("status_body", 404, '{"error": "model \'nope:latest\' not found"}')
    ]
    client = OllamaClient(url, backoff_s=0.01)
    with pytest.raises(OllamaModelMissing, match=r"ollama pull nope"):
        client.generate("hi", model="nope:latest")
    client.close()


def test_loopback_enforcement():
    for bad in (
        "http://example.com:11434",
        "http://192.168.1.10:11434",
        "http://10.0.0.5:11434",
        "https://ollama.example.com",
    ):
        with pytest.raises(OllamaError, match="non-loopback"):
            OllamaClient(bad)


def test_loopback_allowed():
    for good in ("http://127.0.0.1:11434", "http://localhost:11434"):
        OllamaClient(good).close()


def test_unreachable_is_explicit(mock_ollama):
    url, server = mock_ollama
    server.shutdown()  # kill it; the port is now dead
    client = OllamaClient(url, max_retries=0)
    with pytest.raises(OllamaUnreachable, match="ollama serve"):
        client.generate("hi", model="llama3.1")
    client.close()


def test_probe_reports_models(mock_ollama):
    url, server = mock_ollama
    server.script["tags"] = [
        ("json", {"models": [{"name": "llama3.1:latest"}, {"name": "qwen2.5:7b"}]})
    ]
    status = probe(url)
    assert status["reachable"] is True
    assert "llama3.1:latest" in status["recommended"]


def test_timeout_is_explicit(mock_ollama):
    url, server = mock_ollama
    server.script["generate"] = [("hang",)]
    client = OllamaClient(url, timeout_s=1.0, max_retries=0, backoff_s=0.01)
    with pytest.raises(OllamaUnreachable):
        client.generate("hi", model="llama3.1", timeout_s=1.0)
    client.close()


def test_probe_unreachable_reports_error():
    status = probe("http://127.0.0.1:1")  # nothing listens here
    assert status["reachable"] is False
    assert status["error"]
