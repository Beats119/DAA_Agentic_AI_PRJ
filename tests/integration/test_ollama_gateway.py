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
from autograder.app.ollama.health import probe


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
    assert record.prompt_version == "1.1"  # from prompts/specification.txt
    assert record.prompt_hash.startswith("sha256:")
    # No Authorization header was sent.
    assert b"authorization" not in server.last_request_body.lower()
    client.close()


def test_generate_sends_num_predict_and_records_tokens(mock_ollama):
    """D27 + D10: the configured max_tokens reaches Ollama as
    ``options.num_predict``, and the server's eval counts land on the
    GenerationRecord as tokens_used."""
    url, server = mock_ollama
    server.script["generate"] = [
        ("json", {"response": '{"name": "x", "score": 0.5}',
                  "prompt_eval_count": 120, "eval_count": 30})
    ]
    client = OllamaClient(url)
    item, record = client.generate_structured(
        task="t", prompt="p", schema=_Item, model_cfg=_cfg(max_tokens=777),
        prompt_name="specification")
    client.close()
    body = json.loads(server.last_request_body)
    assert body["options"]["num_predict"] == 777
    assert record.tokens_used == 150


def test_generate_without_max_tokens_omits_num_predict_and_estimates(mock_ollama):
    url, server = mock_ollama
    response = '{"name": "x", "score": 0.5}'
    server.script["generate"] = [("json", {"response": response})]
    client = OllamaClient(url)
    item, record = client.generate_structured(
        task="t", prompt="p", schema=_Item, model_cfg=_cfg(),
        prompt_name="specification")
    client.close()
    body = json.loads(server.last_request_body)
    assert "num_predict" not in body["options"]
    # No eval counts in the body -> documented len//4 estimate per side.
    assert record.tokens_used == len("p") // 4 + len(response) // 4
    assert record.tokens_used > 0


def test_tokens_accumulate_across_validation_retries(mock_ollama):
    """D10: usage from FAILED attempts counts too — the budget reflects
    what was actually spent."""
    url, server = mock_ollama
    server.script["generate"] = [
        ("json", {"response": "not json",
                  "prompt_eval_count": 10, "eval_count": 5}),
        ("json", {"response": '{"name": "x", "score": 0.5}',
                  "prompt_eval_count": 20, "eval_count": 7}),
    ]
    client = OllamaClient(url)
    item, record = client.generate_structured(
        task="t", prompt="p", schema=_Item, model_cfg=_cfg(),
        prompt_name="specification")
    client.close()
    assert item.name == "x"
    assert record.tokens_used == 15 + 27


def test_500_is_not_retried(mock_ollama):
    """A 5xx means the request reached the server; repeating an
    expensive generation cannot fix it, so exactly ONE request is made
    and the error surfaces immediately (connect-class failures are the
    only retried class — see the client policy)."""
    url, server = mock_ollama
    server.script["generate"] = [
        ("status", 500),
        ("json", {"response": '{"name": "x", "score": 0.5}'}),
    ]
    client = OllamaClient(url, backoff_s=0.01)
    with pytest.raises(OllamaError, match="HTTP 500"):
        client.generate("hi", model="llama3.1")
    client.close()
    # The scripted success was never consumed: only one request happened.
    assert len(server.script["generate"]) == 1


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
    server.shutdown()  # stop the serve loop...
    server.server_close()  # ...and close the listening socket: the port is now truly dead
    client = OllamaClient(url, connect_retries=0)
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
    client = OllamaClient(url, timeout_s=1.0, backoff_s=0.01)
    with pytest.raises(OllamaUnreachable):
        client.generate("hi", model="llama3.1", timeout_s=1.0)
    client.close()


def test_probe_unreachable_reports_error():
    status = probe("http://127.0.0.1:1")  # nothing listens here
    assert status["reachable"] is False
    assert status["error"]
