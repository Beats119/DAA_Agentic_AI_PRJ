"""Streaming Ollama client: NDJSON assembly, idle-timeout semantics,
the connect-only retry policy, warm-up, and the models.yaml contract.

All transports here are TEST-ONLY doubles (httpx MockTransport); no
real server is involved. The policy under test, from the run data that
motivated it (local llama3.1 at ~1-3 tok/s, ~90s cold loads, feedback
burning ~1090s on multiplied retries):

- generation is streamed, so ``timeout_s`` is an idle gap limit, never
  a total-duration cap;
- ONLY connect-class failures are retried (once) — a failed connect
  produced no inference, so nothing is duplicated;
- read timeouts, HTTP errors and bad bodies surface immediately.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from autograder.app.ollama.client import (
    OllamaBadResponse,
    OllamaClient,
    OllamaError,
    OllamaUnreachable,
)
from autograder.app.ollama.models import model_cfg_for
from tests.integration.test_orchestration import (  # noqa: F401  (fixtures)
    FakeGateway,
    assignment_pdf,
    rules_path,
    source_path,
)


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    score: float


def _client(handler, **kwargs) -> OllamaClient:
    """An OllamaClient wired to a MockTransport (test double)."""
    kwargs.setdefault("backoff_s", 0.0)
    client = OllamaClient("http://127.0.0.1:11434", **kwargs)
    client._transport = httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False)
    return client


def _ndjson(*chunks: dict) -> str:
    return "".join(json.dumps(c) + "\n" for c in chunks)


_OK_STREAM = _ndjson(
    {"response": "o"},
    {"response": "k", "done": True, "eval_count": 1, "prompt_eval_count": 2},
)


def _attempt_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "autograder.llm" and "http attempt" in r.getMessage()]


# --------------------------------------------------------------------- #
# Streaming assembly + token accounting + ttft telemetry
# --------------------------------------------------------------------- #

def test_streaming_assembles_chunks_and_counts_tokens(caplog):
    payload = '{"name": "x", "score": 0.5}'
    body = _ndjson(
        {"response": payload[:10]},
        {"response": payload[10:20]},
        {"response": payload[20:],
         "done": True, "eval_count": 30, "prompt_eval_count": 120,
         "eval_duration": 2_500_000_000, "total_duration": 3_000_000_000,
         "load_duration": 100_000_000},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    client = _client(handler)
    with caplog.at_level(logging.INFO, logger="autograder.llm"):
        item, record = client.generate_structured(
            task="specification", prompt="p", schema=_Item,
            model_cfg={"model": "llama3.1", "temperature": 0.0, "seed": 42,
                       "timeout_s": 5.0, "max_tokens": 2048,
                       "num_ctx": 8192},
        )
    client.close()
    assert item.name == "x" and item.score == 0.5
    # Tokens come from the FINAL chunk's statistics, as before.
    assert record.tokens_used == 150
    got = [r.getMessage() for r in caplog.records
           if r.name == "autograder.llm" and "←" in r.getMessage()]
    assert got, "no response line logged"
    assert "ttft=" in got[0]
    assert "tokens=30" in got[0]
    assert "gen=2.5s" in got[0]
    assert "total=3.0s" in got[0]


def test_generate_sends_streaming_payload_with_ctx_and_keep_alive():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=_OK_STREAM)

    client = _client(handler)
    text = client.generate(
        "hi", model="llama3.1", num_predict=64, num_ctx=4096,
        keep_alive="5m")
    client.close()
    assert text == "ok"
    assert seen["stream"] is True
    assert seen["keep_alive"] == "5m"
    assert seen["options"]["num_ctx"] == 4096
    assert seen["options"]["num_predict"] == 64


def test_empty_stream_is_bad_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_ndjson({"response": "",
                                                 "done": True}))

    client = _client(handler)
    with pytest.raises(OllamaBadResponse):
        client.generate("hi", model="llama3.1")
    client.close()


# --------------------------------------------------------------------- #
# Retry policy: connect-class only, exactly one retry
# --------------------------------------------------------------------- #

def test_connect_error_then_success_recovers_with_two_attempts(caplog):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, text=_OK_STREAM)

    client = _client(handler)
    with caplog.at_level(logging.INFO, logger="autograder.llm"):
        assert client.generate("hi", model="llama3.1") == "ok"
    client.close()
    assert calls["n"] == 2
    lines = _attempt_lines(caplog)
    assert len(lines) == 2, lines
    assert "attempt 1/2" in lines[0] and "ConnectError" in lines[0]
    assert "attempt 2/2" in lines[1] and "ok (HTTP 200)" in lines[1]


def test_connect_errors_exhaust_single_retry(caplog):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with caplog.at_level(logging.INFO, logger="autograder.llm"):
        with pytest.raises(OllamaUnreachable, match="ollama serve"):
            client.generate("hi", model="llama3.1")
    client.close()
    assert calls["n"] == 2  # initial attempt + exactly ONE retry
    lines = _attempt_lines(caplog)
    assert len(lines) == 2
    assert all("ConnectError" in line for line in lines)


def test_read_timeout_is_single_attempt_unreachable(caplog):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("no chunk within the idle window")

    client = _client(handler)
    with caplog.at_level(logging.INFO, logger="autograder.llm"):
        with pytest.raises(OllamaUnreachable):
            client.generate("hi", model="llama3.1")
    client.close()
    # A stalled generation is NEVER re-issued: exactly one attempt.
    assert calls["n"] == 1
    lines = _attempt_lines(caplog)
    assert len(lines) == 1, lines
    assert "attempt 1/1" in lines[0] and "ReadTimeout" in lines[0]


def test_http_500_is_single_attempt(caplog):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    client = _client(handler)
    with caplog.at_level(logging.INFO, logger="autograder.llm"):
        with pytest.raises(OllamaError, match="HTTP 500"):
            client.generate("hi", model="llama3.1")
    client.close()
    assert calls["n"] == 1
    lines = _attempt_lines(caplog)
    assert len(lines) == 1, lines
    assert "attempt 1/1" in lines[0] and "HTTP 500" in lines[0]


# --------------------------------------------------------------------- #
# Warm-up
# --------------------------------------------------------------------- #

def test_warmup_uses_tiny_pinned_generation():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=_OK_STREAM)

    client = _client(handler)
    elapsed = client.warmup("llama3.1")
    client.close()
    assert isinstance(elapsed, float) and elapsed >= 0.0
    assert seen["prompt"] == "OK"
    assert seen["stream"] is True
    assert seen["keep_alive"] == "30m"
    assert seen["options"]["num_predict"] == 1
    assert seen["options"]["num_ctx"] == 8192


def test_warmup_failure_raises_for_caller_to_tolerate():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(OllamaUnreachable):
        client.warmup("llama3.1")
    client.close()


# --------------------------------------------------------------------- #
# Workflow warm-up: used when offered, tolerated when it fails
# --------------------------------------------------------------------- #

class _WarmGateway(FakeGateway):
    """TEST-ONLY gateway that records warm-up calls."""

    def __init__(self) -> None:
        super().__init__()
        self.warmup_calls: list[str] = []

    def warmup(self, model: str) -> float:
        self.warmup_calls.append(model)
        return 0.25


class _WarmFailGateway(FakeGateway):
    """TEST-ONLY gateway whose warm-up always fails."""

    def warmup(self, model: str) -> float:
        raise RuntimeError("ollama down")


def test_workflow_warms_up_gateway_before_specification(
        assignment_pdf, rules_path, source_path, tmp_path):  # noqa: F811
    from autograder.app.orchestration.workflow import evaluate

    gateway = _WarmGateway()
    result = evaluate(
        str(assignment_pdf), str(source_path), str(rules_path),
        gateway=gateway, run_id="warm_ok_01",
        base_dir=str(tmp_path / "artifacts"), max_workers=4)
    assert result.status == "completed", result.report.limitations
    assert gateway.warmup_calls == ["llama3.1"]  # the specification model


def test_workflow_tolerates_warmup_failure(
        assignment_pdf, rules_path, source_path, tmp_path,  # noqa: F811
        caplog):
    from autograder.app.orchestration.workflow import evaluate

    with caplog.at_level(logging.WARNING, logger="autograder.workflow"):
        result = evaluate(
            str(assignment_pdf), str(source_path), str(rules_path),
            gateway=_WarmFailGateway(), run_id="warm_fail_01",
            base_dir=str(tmp_path / "artifacts"), max_workers=4)
    # The run completes on the gateway's task calls; warm-up is advisory.
    assert result.status == "completed", result.report.limitations
    warnings = [r.getMessage() for r in caplog.records
                if r.name == "autograder.workflow"
                and r.levelno >= logging.WARNING]
    assert any("warm-up failed" in m for m in warnings), warnings


# --------------------------------------------------------------------- #
# models.yaml contract
# --------------------------------------------------------------------- #

def test_models_yaml_streaming_contract():
    expected_caps = {
        "specification": 2048,
        "test_generation": 2048,
        "test_refinement": 2048,
        "code_review": 1024,
        "feedback": 1024,
        "feedback_check": 1024,
    }
    expected_idle = {
        "specification": 180,
        "test_generation": 180,
        "test_refinement": 180,
        "code_review": 120,
        "feedback": 120,
        "feedback_check": 120,
    }
    for task in expected_caps:
        cfg = model_cfg_for(task)
        assert cfg["num_ctx"] == 8192, task
        assert cfg["max_tokens"] == expected_caps[task], task
        assert cfg["timeout_s"] == expected_idle[task], task
