"""Instrumentation guarantees: per-attempt HTTP logs, coordinator
worker-log discoverability, and per-stage timings in the run manifest.

Guarantees:
- every HTTP attempt in the streaming Ollama client emits exactly one
  INFO line on ``autograder.llm`` with its attempt number, applicable
  maximum, path, outcome, and elapsed seconds — and never any
  prompt/payload content;
- every finished analyzer job logs its worker stderr-log path on
  ``autograder.workflow``;
- ``RunManifest.stage_timings`` is additive: old manifests (without
  the field) still validate, and a full evaluation persists per-stage
  timings in workflow order.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from autograder.app.core.schemas import RunManifest
from autograder.app.ollama.client import OllamaClient
from tests.integration.test_orchestration import (  # noqa: F401  (pytest fixtures)
    FakeGateway,
    assignment_pdf,
    rules_path,
    source_path,
)


# --------------------------------------------------------------------- #
# (a) per-attempt HTTP logging in the Ollama client
# --------------------------------------------------------------------- #

class _StubRequest:
    headers: dict = {}


class _StubResponse:
    """Minimal streamed response: one NDJSON chunk, then done."""

    status_code = 200

    def close(self):
        return None

    def read(self):
        return b""

    def iter_lines(self):
        yield json.dumps({
            "response": "ok", "done": True,
            "eval_count": 1, "prompt_eval_count": 1,
        })

    def raise_for_status(self):
        return None


class _FlakyTransport:
    """Raises ConnectError for the first ``failures`` sends, then answers.

    Connect-class failures are the only retried class under the current
    client policy, so they are what exercises the retry path here.
    """

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def build_request(self, method, url, json=None, timeout=None):
        return _StubRequest()

    def send(self, req, stream=False):
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ConnectError("connection refused")
        return _StubResponse()


def _client(transport) -> OllamaClient:
    client = OllamaClient("http://127.0.0.1:11434", backoff_s=0.0)
    client._transport = transport
    return client


def _attempt_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "autograder.llm" and "http attempt" in r.getMessage()]


def test_http_attempts_logged_per_attempt(caplog):
    # One connect failure (retried: the only retryable class), then OK.
    client = _client(_FlakyTransport(failures=1))
    with caplog.at_level(logging.INFO, logger="autograder.llm"):
        assert client.generate("hello", model="llama3.1") == "ok"
    lines = _attempt_lines(caplog)
    assert len(lines) == 2, lines
    assert "attempt 1/2" in lines[0] and "ConnectError" in lines[0]
    assert "attempt 2/2" in lines[1] and "ok (HTTP 200)" in lines[1]
    assert all("/api/generate" in line for line in lines)
    # Attempt lines carry outcome + timing only — never prompt content.
    assert all("hello" not in line for line in lines)


def test_http_attempt_logged_once_on_immediate_success(caplog):
    client = _client(_FlakyTransport(failures=0))
    with caplog.at_level(logging.INFO, logger="autograder.llm"):
        assert client.generate("hello", model="llama3.1") == "ok"
    lines = _attempt_lines(caplog)
    assert len(lines) == 1, lines
    assert "attempt 1/1" in lines[0] and "ok (HTTP 200)" in lines[0]


# --------------------------------------------------------------------- #
# (b) coordinator worker-log discoverability
# --------------------------------------------------------------------- #

def test_coordinator_logs_worker_log_path(tmp_path, caplog):
    from autograder.app.analyzers.base import AnalyzerContext
    from autograder.app.core.schemas import MetricConfig
    from autograder.app.orchestration.coordinator import (
        AnalyzerJob,
        run_analyzers,
    )
    from autograder.app.rules.loader import load_rules

    repo = Path(__file__).resolve().parents[2]
    rules = load_rules(repo / "examples" / "rules" / "add.json")
    ctx = AnalyzerContext(
        source_path=str(tmp_path / "submission.c"),
        binary_path=None,
        sanitizer_binary_path=None,
        rules=rules,
        metric=MetricConfig(name="fast_metric", analyzer="test_only",
                            weight=0.5),
        run_dir=str(tmp_path),
        executor=None,
        compiler=None,
        run_id="instrumentation_test",
    )
    job = AnalyzerJob(
        metric_name="fast_metric",
        analyzer_id="test_only",
        module_path="tests.integration.deadline_analyzers:fast_analyze",
        ctx=ctx,
        timeout_s=30.0,
    )
    with caplog.at_level(logging.INFO, logger="autograder.workflow"):
        outcomes = run_analyzers([job], max_workers=1)
    assert outcomes["fast_metric"].result.status == "completed"

    lines = [r.getMessage() for r in caplog.records
             if r.name == "autograder.workflow"
             and "worker log:" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "fast_metric" in lines[0]
    assert "stderr.log" in lines[0]


# --------------------------------------------------------------------- #
# (c) manifest stage timings: schema compat + end-to-end persistence
# --------------------------------------------------------------------- #

_MINIMAL_MANIFEST = {
    "run_id": "abc123",
    "state": "completed",
    "submission_hash": "sha256:aaa",
    "assignment_hash": "sha256:bbb",
    "rules_hash": "sha256:ccc",
    "rules_version": "1.0",
}


def test_manifest_stage_timings_optional_and_roundtrip():
    # Backward compat: a manifest written before the field existed
    # (no stage_timings key) still validates, defaulting to empty.
    legacy = RunManifest.model_validate(_MINIMAL_MANIFEST)
    assert legacy.stage_timings == []

    payload = dict(_MINIMAL_MANIFEST, stage_timings=[
        {"stage": "validating",
         "started_at": "2026-10-21T00:00:00Z", "duration_s": 0.25},
        {"stage": "reporting",
         "started_at": "2026-10-21T00:01:00Z", "duration_s": 1.5},
    ])
    manifest = RunManifest.model_validate(payload)
    assert [t.stage for t in manifest.stage_timings] == [
        "validating", "reporting"]
    assert manifest.stage_timings[0].duration_s == 0.25

    dumped = json.loads(manifest.model_dump_json())
    assert dumped["stage_timings"][1]["stage"] == "reporting"
    # Full JSON round-trip keeps the timings intact.
    again = RunManifest.model_validate_json(manifest.model_dump_json())
    assert [t.stage for t in again.stage_timings] == [
        "validating", "reporting"]


def test_manifest_records_stage_timings_end_to_end(
        assignment_pdf, rules_path, source_path, tmp_path):  # noqa: F811
    from autograder.app.orchestration.workflow import evaluate

    result = evaluate(
        str(assignment_pdf), str(source_path), str(rules_path),
        gateway=FakeGateway(), run_id="instrumentation_e2e",
        base_dir=str(tmp_path / "artifacts"), max_workers=4)
    assert result.status == "completed", result.report.limitations

    manifest = json.loads(
        (Path(result.artifacts_dir) / "manifest.json").read_text())
    timings = manifest["stage_timings"]
    assert timings, "stage_timings missing or empty in persisted manifest"

    canonical = ["validating", "planning", "specification",
                 "generating_tests", "oracle_assessment", "compiling",
                 "analyzing", "scoring", "feedback", "reporting"]
    stages = [t["stage"] for t in timings]
    positions = [canonical.index(stage) for stage in stages]
    assert positions == sorted(positions), stages  # workflow order
    assert stages[0] == "validating"
    assert "analyzing" in stages and "feedback" in stages
    assert "reporting" in stages
    assert all(t["duration_s"] >= 0 for t in timings)
    assert all(t["started_at"] for t in timings)

    # The persisted manifest still validates against the current schema.
    RunManifest.model_validate(manifest)
