"""Benchmark CLI (latency Finding 5): collection wiring + honest preflight.

The benchmark must never fabricate results: with no Ollama it exits
nonzero with a one-line error. The collection path is proven in-process
through the production run_evaluation with a TEST-ONLY fake gateway.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autograder.app import benchmark
from autograder.app.core.schemas import RunDiagnostics, RunManifest, StageDiagnostics


def test_format_table_on_fabricated_document():
    doc = {
        "cold_note": None,
        "repetitions": [
            {"label": "cold", "wall_s": 95.4, "status": "completed",
             "grade": 88.0, "tests_trusted": 3, "llm_calls": 7,
             "output_tokens": 2100},
            {"label": "warm", "wall_s": 41.2, "status": "incomplete",
             "grade": None, "tests_trusted": 0, "llm_calls": 5,
             "output_tokens": 900},
        ],
    }
    table = benchmark.format_table(doc)
    assert "cold" in table and "95.4" in table and "88.0" in table
    assert "warm" in table and "incomplete" in table


def test_collect_run_reads_manifest_diagnostics(tmp_path):
    diag = RunDiagnostics(
        stages={"specification": StageDiagnostics(
            llm_calls=1, prompt_tokens=900, output_tokens=300,
            tokens_used=1200, wall_s=12.5)},
        oracle_trusted=2, tier3_skipped_by_policy=5, generation_rounds=2,
        sandbox_executions=9,
    )
    manifest = RunManifest(
        run_id="bench1", state="completed", submission_hash="s",
        assignment_hash="a", rules_hash="r", rules_version="1",
        diagnostics=diag,
    )
    art = tmp_path / "runs" / "bench1"
    art.mkdir(parents=True)
    (art / "manifest.json").write_text(manifest.model_dump_json())

    class _Report:
        grade = 91.0

        class test_stats:  # noqa: N801 - mirrors Report.test_stats shape
            generated = 6
            trusted = 2

    class _Result:
        run_id = "bench1"
        status = "completed"
        artifacts_dir = str(art)
        report = _Report()

    row = benchmark.collect_run(_Result(), wall_s=33.3, label="warm")
    assert row["wall_s"] == 33.3 and row["grade"] == 91.0
    assert row["tests_trusted"] == 2
    assert row["llm_calls"] == 1 and row["output_tokens"] == 300
    assert row["oracle"]["tier3_skipped_by_policy"] == 5
    assert row["generation_rounds"] == 2
    assert row["sandbox_executions"] == 9
    assert row["stages"]["specification"]["wall_s"] == 12.5


def test_preflight_reports_unreachable(monkeypatch):
    monkeypatch.setattr(
        benchmark, "ollama_probe",
        lambda url: {"reachable": False, "error": "connection refused"})
    error = benchmark.preflight_error("http://127.0.0.1:11434")
    assert error is not None and "not reachable" in error
    with pytest.raises(benchmark.BenchmarkError):
        benchmark.run_benchmark("a", "s", "r")


def test_main_exits_nonzero_without_ollama(monkeypatch, capsys):
    monkeypatch.setattr(
        benchmark, "ollama_probe",
        lambda url: {"reachable": False, "error": "connection refused"})
    rc = benchmark.main(["--assignment", "a", "--source", "s",
                         "--rules", "r"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not reachable" in err
    assert "Traceback" not in err


def test_run_benchmark_collects_with_fake_gateway(tmp_path):
    """In-process wiring proof: production evaluate() via run_evaluation."""
    repo = Path(__file__).resolve().parents[2]
    import sys
    if str(repo / "tests" / "integration") not in sys.path:
        sys.path.insert(0, str(repo / "tests" / "integration"))
    from test_orchestration import FakeGateway  # noqa: E402

    assignment = tmp_path / "assignment.txt"
    assignment.write_text(
        "Assignment: Add Two Integers\n\nWrite a C program that reads "
        "two integers from standard input and prints their sum followed "
        "by a newline.\n\nExample:\nInput:\n2 3\nOutput:\n5\n")
    rules_src = json.loads((repo / "examples" / "rules" / "add.json")
                           .read_text())
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(rules_src))
    source = repo / "tests" / "fixtures" / "c" / "correct_add.c"

    doc = benchmark.run_benchmark(
        assignment, source, rules, repetitions=1,
        base_dir=tmp_path / "bench", gateway=FakeGateway())
    rep = doc["repetitions"][0]
    assert rep["status"] in ("completed", "completed_with_warnings")
    assert rep["tests_trusted"] >= 1
    assert rep["llm_calls"] >= 3  # spec + generation + feedback at least
    assert rep["label"] == "warm"
    table = benchmark.format_table(doc)
    assert "trusted" in table
