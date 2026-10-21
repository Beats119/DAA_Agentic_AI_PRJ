"""Terminal logging tests.

Three guarantees:
- every gateway model call emits a send line and a response line (with
  timing) on the ``autograder.llm`` logger — against a test-double
  transport, no real server involved;
- ``configure_logging`` honors ``AUTOGRADER_LOG_LEVEL``, defaults to
  INFO, falls back to INFO on garbage, and is idempotent;
- a full workflow run (the fake-gateway E2E path from the orchestration
  tests) emits per-step progress lines on ``autograder.workflow``,
  including the test-generation step and per-analyzer lines.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from autograder.app.logging_config import configure_logging
from autograder.app.ollama.client import OllamaClient, OllamaError
from tests.integration.test_orchestration import (  # noqa: F401  (pytest fixtures)
    FakeGateway,
    assignment_pdf,
    rules_path,
    source_path,
)


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    score: float


def _client(handler) -> OllamaClient:
    """An OllamaClient wired to a MockTransport (test double)."""
    client = OllamaClient("http://127.0.0.1:11434")
    client._transport = httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False)
    return client


# --------------------------------------------------------------------- #
# (a) gateway logging
# --------------------------------------------------------------------- #

def test_gateway_logs_send_and_response(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "response": '{"name": "x", "score": 0.5}',
            "prompt_eval_count": 120,
            "eval_count": 30,
            "eval_duration": 2_500_000_000,
            "total_duration": 3_000_000_000,
        })

    client = _client(handler)
    with caplog.at_level(logging.INFO, logger="autograder.llm"):
        item, _record = client.generate_structured(
            task="test_generation", prompt="write the tests", schema=_Item,
            model_cfg={"model": "llama3.1", "temperature": 0.0,
                       "seed": 42, "timeout_s": 5.0},
        )
    client.close()
    assert item.name == "x"

    records = [r for r in caplog.records if r.name == "autograder.llm"]
    sent = [r.getMessage() for r in records if "→" in r.getMessage()]
    got = [r.getMessage() for r in records if "←" in r.getMessage()]
    assert sent, "no send line logged"
    assert "task=test_generation" in sent[0]
    assert "model=llama3.1" in sent[0]
    assert "prompt=15 chars" in sent[0]
    assert got, "no response line logged"
    assert "task=test_generation" in got[0]
    assert "response=" in got[0] and "chars" in got[0]
    # Ollama's own timing fields are surfaced in friendly units.
    assert "tokens=30" in got[0]
    assert "gen=2.5s" in got[0]
    assert "total=3.0s" in got[0]
    assert all(r.levelno == logging.INFO for r in records)


def test_gateway_logs_failure_with_elapsed(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _client(handler)
    with caplog.at_level(logging.INFO, logger="autograder.llm"):
        with pytest.raises(OllamaError):
            client.generate("hi", model="llama3.1")
    client.close()

    failures = [r for r in caplog.records
                if r.name == "autograder.llm" and r.levelno >= logging.WARNING]
    assert failures, "no failure line logged"
    assert "task=generate" in failures[0].getMessage()
    assert "failed after" in failures[0].getMessage()


# --------------------------------------------------------------------- #
# (b) configure_logging
# --------------------------------------------------------------------- #

@pytest.fixture()
def _clean_logging():
    """Remove the marked handler and restore the root level afterwards."""
    root = logging.getLogger()
    old_level = root.level
    yield
    for handler in list(root.handlers):
        if getattr(handler, "_autograder_handler", False):
            root.removeHandler(handler)
    root.setLevel(old_level)


def test_configure_logging_defaults_to_info(monkeypatch, _clean_logging):
    monkeypatch.delenv("AUTOGRADER_LOG_LEVEL", raising=False)
    assert configure_logging() == logging.INFO
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_env_and_explicit_level(monkeypatch, _clean_logging):
    monkeypatch.setenv("AUTOGRADER_LOG_LEVEL", "debug")
    assert configure_logging() == logging.DEBUG
    # An explicit argument beats the environment.
    assert configure_logging("error") == logging.ERROR
    assert logging.getLogger().level == logging.ERROR


def test_configure_logging_garbage_falls_back_to_info(monkeypatch,
                                                      _clean_logging):
    monkeypatch.setenv("AUTOGRADER_LOG_LEVEL", "shouty")
    assert configure_logging() == logging.INFO


def test_configure_logging_is_idempotent(monkeypatch, _clean_logging):
    monkeypatch.delenv("AUTOGRADER_LOG_LEVEL", raising=False)
    configure_logging()
    configure_logging()
    marked = [h for h in logging.getLogger().handlers
              if getattr(h, "_autograder_handler", False)]
    assert len(marked) == 1


# --------------------------------------------------------------------- #
# (c) workflow step logging
# --------------------------------------------------------------------- #

def test_workflow_logs_steps(assignment_pdf, rules_path, source_path,  # noqa: F811
                             tmp_path, caplog):
    from autograder.app.orchestration.workflow import evaluate

    with caplog.at_level(logging.INFO, logger="autograder.workflow"):
        result = evaluate(
            str(assignment_pdf), str(source_path), str(rules_path),
            gateway=FakeGateway(), run_id="logsteps01",
            base_dir=str(tmp_path / "artifacts"), max_workers=4)
    assert result.status == "completed", result.report.limitations

    messages = [r.getMessage() for r in caplog.records
                if r.name == "autograder.workflow"]
    assert messages, "no workflow log records captured"
    step_names = {m.split(" step ")[1].split(" ")[0]
                  for m in messages if " step " in m}
    # The pipeline's major phases all report in — including the one the
    # user actually waits on (test generation against the LLM).
    assert "generating_tests" in step_names
    assert len(step_names) >= 5, step_names
    assert any(m.startswith("generation round 1:") for m in messages)
    assert any("oracle assessment:" in m for m in messages)
    analyzer_lines = [m for m in messages if m.startswith("analyzer ")]
    assert len(analyzer_lines) >= 3, analyzer_lines
    assert any("finished: completed" in m for m in messages)
