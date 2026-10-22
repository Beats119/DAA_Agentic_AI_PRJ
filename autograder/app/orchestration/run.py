"""Thin runner: wire a gateway (or None), run the workflow, handle failures.

Creates the run directory, probes Ollama, and calls
:func:`autograder.app.orchestration.workflow.evaluate`. Top-level
exceptions become a ``failed`` run record with diagnostics — never a
traceback with server paths in the report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from ..logging_config import configure_logging
from ..ollama.client import OllamaClient
from ..ollama.health import probe as ollama_probe
from ..persistence.store import RunStore
from .workflow import RunResult, WorkflowError, evaluate


def make_gateway(base_url: str | None = None) -> tuple[Any | None, str | None]:
    """Return ``(gateway, limitation)``.

    Probes the local Ollama server first; when unreachable, returns
    ``(None, reason)`` so the workflow takes the degraded (no-LLM) path
    instead of failing.
    """
    url = base_url or os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    try:
        info = ollama_probe(url)
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        return None, f"Ollama probe failed ({exc}); continuing without LLM"
    if not info.get("reachable"):
        return None, (
            f"Ollama not reachable at {url} "
            f"({info.get('error', 'unknown error')}); continuing without LLM"
        )
    try:
        return OllamaClient(url), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not create Ollama client ({exc}); continuing without LLM"


def run_evaluation(
    assignment: str | Path,
    source: str | Path,
    rules: str | Path,
    *,
    gateway: Any | None = None,
    gateway_url: str | None = None,
    run_id: str | None = None,
    base_dir: str | Path | None = None,
    max_workers: int = 4,
    evaluation_timeout_s: float = 1800.0,
    force_regenerate: bool = False,
    _probe_gateway: bool = True,
) -> RunResult:
    """Run one evaluation, wiring the gateway unless one is supplied."""
    limitation: str | None = None
    if gateway is None and _probe_gateway:
        gateway, limitation = make_gateway(gateway_url)
    if limitation:
        print(f"[autograder] note: {limitation}", file=sys.stderr)
    return evaluate(
        assignment,
        source,
        rules,
        gateway=gateway,
        run_id=run_id,
        base_dir=base_dir,
        max_workers=max_workers,
        evaluation_timeout_s=evaluation_timeout_s,
        force_regenerate=force_regenerate,
    )


def _failed_record(
    base_dir: Path,
    reason: str,
    run_id: str | None = None,
) -> str:
    """Persist a minimal ``failed`` run record for a pre-workflow failure."""
    import hashlib
    import uuid

    rid = run_id or uuid.uuid4().hex
    store = RunStore(base_dir)
    empty = "sha256:" + hashlib.sha256(b"").hexdigest()
    try:
        store.create_run(empty, empty, empty, "unparsed", run_id=rid)
    except Exception:
        return rid
    try:
        store.write_artifact(
            rid, "failure.txt",
            f"evaluation could not start: {reason}\n".encode(),
        )
    except Exception:
        pass
    try:
        store.transition_run(rid, "validating")
        store.transition_run(rid, "failed")
    except Exception:
        pass
    return rid


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(
        prog="autograder-evaluate",
        description="Run one AutoGrader evaluation (Phases 2-6 pipeline).",
    )
    parser.add_argument("--assignment", required=True,
                        help="assignment PDF (.pdf/.txt/.md)")
    parser.add_argument("--source", required=True, help="student C file")
    parser.add_argument("--rules", required=True, help="rules JSON/YAML")
    parser.add_argument("--ollama-url", default=None,
                        help="Ollama base URL (default: $OLLAMA_URL or loopback)")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip the LLM gateway entirely (degraded path)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--base-dir", default=None,
                        help="artifacts base dir (default: ./artifacts)")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--evaluation-timeout-s", type=float, default=1800.0,
                        help="overall deadline for the analyzer phase")
    parser.add_argument("--force-regenerate", action="store_true",
                        help="bypass the per-assignment cache: extract the "
                             "specification and generate tests fresh for "
                             "this run (a successful fresh result still "
                             "updates the cache)")
    args = parser.parse_args(argv)

    base = Path(args.base_dir or Path.cwd() / "artifacts")
    base.mkdir(parents=True, exist_ok=True)

    try:
        if args.no_llm:
            result = evaluate(
                args.assignment, args.source, args.rules,
                gateway=None, run_id=args.run_id, base_dir=base,
                max_workers=args.max_workers,
                evaluation_timeout_s=args.evaluation_timeout_s,
                force_regenerate=args.force_regenerate,
            )
        else:
            result = run_evaluation(
                args.assignment, args.source, args.rules,
                gateway_url=args.ollama_url, run_id=args.run_id,
                base_dir=base, max_workers=args.max_workers,
                evaluation_timeout_s=args.evaluation_timeout_s,
                force_regenerate=args.force_regenerate,
            )
    except WorkflowError as exc:
        rid = _failed_record(base, str(exc), args.run_id)
        print(json.dumps({"run_id": rid, "status": "failed",
                          "error": str(exc)}))
        return 2
    except Exception as exc:  # noqa: BLE001 - last resort
        rid = _failed_record(
            base,
            f"unexpected error: {type(exc).__name__}: {exc}\n"
            + traceback.format_exc(limit=5),
            args.run_id,
        )
        print(json.dumps({"run_id": rid, "status": "failed",
                          "error": f"{type(exc).__name__}: {exc}"}))
        return 1

    grade = result.report.grade
    print(json.dumps({
        "run_id": result.run_id,
        "status": result.status,
        "grade": grade,
        "artifacts_dir": result.artifacts_dir,
        "feedback_used_fallback": result.feedback_info.get("used_fallback"),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
