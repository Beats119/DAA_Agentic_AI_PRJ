"""Real-model pipeline benchmark: production path, honest numbers.

Runs the PRODUCTION evaluation path (:func:`orchestration.run.run_evaluation`,
same wiring as the CLI) against a real local Ollama server and reports
wall-clock time, per-stage timings, LLM call/token totals (from the run
manifest's diagnostics), trusted-test count, final status and grade —
per repetition, with cold and warm runs labelled separately.

There are no mocked transports and no fabricated results here: if
Ollama is unreachable the command prints one clear error line and
exits nonzero without running anything.

Usage::

    .venv/bin/python -m autograder.app.benchmark \
        --assignment docs/demo/assignment_add.txt \
        --source tests/fixtures/c/correct_add.c \
        --rules examples/rules/add.json \
        [--repetitions 2] [--cold-first] [--out bench.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .core.schemas import RunDiagnostics, RunManifest
from .ollama.health import probe as ollama_probe
from .ollama.models import model_cfg_for
from .orchestration.run import make_gateway, run_evaluation

_DEFAULT_URL = "http://127.0.0.1:11434"


class BenchmarkError(RuntimeError):
    """Preflight failed; no benchmark runs were performed."""


def _ollama_url(gateway_url: str | None) -> str:
    return gateway_url or os.environ.get("OLLAMA_URL", _DEFAULT_URL)


def preflight_error(gateway_url: str | None = None) -> str | None:
    """None when Ollama answers; otherwise the one-line error to show."""
    url = _ollama_url(gateway_url)
    try:
        info = ollama_probe(url)
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return f"error: Ollama probe failed at {url} ({exc}); benchmark aborted"
    if not info.get("reachable"):
        return (
            f"error: Ollama not reachable at {url} "
            f"({info.get('error', 'unknown error')}); "
            "benchmark aborted — start `ollama serve` and retry"
        )
    return None


def collect_run(result: Any, wall_s: float, label: str) -> dict[str, Any]:
    """Extract one repetition's row from a finished RunResult + manifest."""
    diag = RunDiagnostics()
    manifest_path = Path(result.artifacts_dir) / "manifest.json"
    if manifest_path.exists():
        manifest = RunManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8"))
        diag = manifest.diagnostics
    stages = {
        name: {
            "wall_s": round(sd.wall_s, 2),
            "llm_calls": sd.llm_calls,
            "prompt_tokens": sd.prompt_tokens,
            "output_tokens": sd.output_tokens,
            "tokens_used": sd.tokens_used,
        }
        for name, sd in sorted(diag.stages.items())
    }
    report = result.report
    return {
        "label": label,
        "run_id": result.run_id,
        "wall_s": round(wall_s, 2),
        "status": result.status,
        "grade": report.grade,
        "tests_generated": report.test_stats.generated,
        "tests_trusted": report.test_stats.trusted,
        "llm_calls": sum(sd.llm_calls for sd in diag.stages.values()),
        "output_tokens": sum(sd.output_tokens for sd in diag.stages.values()),
        "tokens_used": sum(sd.tokens_used for sd in diag.stages.values()),
        "generation_rounds": diag.generation_rounds,
        "sandbox_executions": diag.sandbox_executions,
        "oracle": {
            "trusted": diag.oracle_trusted,
            "provisional": diag.oracle_provisional,
            "rejected": diag.oracle_rejected,
            "inconclusive": diag.oracle_inconclusive,
            "tier3_skipped_by_policy": diag.tier3_skipped_by_policy,
            "tier3_budget_exhausted": diag.tier3_budget_exhausted,
        },
        "stages": stages,
    }


def _unload_model(gateway: Any) -> str:
    """Ask Ollama to unload the model so the next run pays the cold load."""
    model = str(model_cfg_for("specification")["model"])
    try:
        gateway.generate("OK", model=model, num_predict=1, keep_alive="0s")
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        return f"unload failed ({type(exc).__name__}: {exc}); first run may be warm"
    return f"model {model} unloaded (keep_alive=0s); first run is cold"


def run_benchmark(
    assignment: str | Path,
    source: str | Path,
    rules: str | Path,
    *,
    repetitions: int = 2,
    cold_first: bool = False,
    base_dir: str | Path | None = None,
    gateway: Any | None = None,
    gateway_url: str | None = None,
) -> dict[str, Any]:
    """Run ``repetitions`` production evaluations and build the report doc.

    With no gateway injected (the CLI path), Ollama reachability is
    preflighted and :class:`BenchmarkError` raised when unreachable.
    An injected gateway (tests) skips the preflight.
    """
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    if gateway is None:
        error = preflight_error(gateway_url)
        if error is not None:
            raise BenchmarkError(error)
    artifacts = Path(base_dir) if base_dir is not None else Path(
        tempfile.mkdtemp(prefix="ag_bench_"))
    cold_note: str | None = None
    if cold_first:
        gw = gateway
        if gw is None:
            gw, limitation = make_gateway(_ollama_url(gateway_url))
            if limitation is not None:
                raise BenchmarkError(limitation)
        cold_note = _unload_model(gw)
    reps: list[dict[str, Any]] = []
    for i in range(repetitions):
        label = "cold" if (cold_first and i == 0) else "warm"
        t0 = time.monotonic()
        result = run_evaluation(
            assignment, source, rules,
            gateway=gateway, gateway_url=gateway_url,
            base_dir=artifacts,
        )
        reps.append(collect_run(result, time.monotonic() - t0, label))
    return {
        "assignment": str(assignment),
        "source": str(source),
        "rules": str(rules),
        "artifacts_dir": str(artifacts),
        "cold_first": cold_first,
        "cold_note": cold_note,
        "repetitions": reps,
    }


def format_table(doc: dict[str, Any]) -> str:
    """Compact human-readable summary of a benchmark document."""
    lines = [
        "rep  label  wall_s  status                    grade  trusted  "
        "llm_calls  out_tokens",
    ]
    for i, rep in enumerate(doc.get("repetitions", []), start=1):
        grade = "-" if rep["grade"] is None else f"{rep['grade']:.1f}"
        lines.append(
            f"{i:<4} {rep['label']:<6} {rep['wall_s']:<7.1f} "
            f"{rep['status']:<25} {grade:<6} {rep['tests_trusted']:<8} "
            f"{rep['llm_calls']:<10} {rep['output_tokens']}"
        )
    if doc.get("cold_note"):
        lines.append(f"note: {doc['cold_note']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autograder-benchmark",
        description="Benchmark the production evaluation pipeline "
                    "against the real local Ollama server.")
    parser.add_argument("--assignment", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--rules", required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--cold-first", action="store_true",
                        help="unload the model first so repetition 1 is cold")
    parser.add_argument("--out", default=None,
                        help="write the JSON report here (default: stdout)")
    args = parser.parse_args(argv)

    error = preflight_error()
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    doc = run_benchmark(
        args.assignment, args.source, args.rules,
        repetitions=args.repetitions, cold_first=args.cold_first,
    )
    print(format_table(doc))
    payload = json.dumps(doc, indent=1)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"json: {args.out}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
