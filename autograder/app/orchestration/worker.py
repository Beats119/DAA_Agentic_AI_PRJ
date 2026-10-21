"""Analyzer worker: runs ONE analyzer in a dedicated subprocess.

Usage::

    python -m autograder.app.orchestration.worker <spec.json> <result.json>

The coordinator (``coordinator.py``) spawns one worker per analyzer so a
hung or crashing analyzer can be *actually* terminated — threads cannot
be killed, processes can. The worker:

1. reads the spec (analyzer module path + the AnalyzerContext as plain
   data, see :meth:`AnalyzerContext.to_spec`),
2. rebuilds the live context in-process (sandbox executor, gateway,
   budget tracker seeded from the run's counters),
3. installs a SIGTERM handler that kills its whole descendant tree
   before exiting — sandbox launchers run in their own sessions, so a
   process-group kill from the coordinator would orphan them; the
   /proc descendant walk closes that hole,
4. runs the analyzer and writes a JSON envelope to the result path:
   ``{"ok": true, "result": ..., "evidence": [...], "budget": {...}}`` on
   success, ``{"ok": false, "error_type": ..., "message": ...,
   "traceback": ...}`` on any Python-level failure, exiting 0 either way.
   A hard crash (``os._exit``, segfault) writes nothing — the coordinator
   detects the missing envelope and records the exit code instead.

GATEWAY FACTORY SEAM: the spec's ``gateway`` field names a factory as a
dotted path (``"module:attr"`` or ``"module.attr"``) plus kwargs. In
production this is always :func:`make_ollama_gateway` (wired by the
workflow from the live client's settings). Tests may name a test-double
factory the same way — that is a *test seam*, not a production feature:
no production code path ever names a non-Ollama factory.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import sys
import traceback
from pathlib import Path
from typing import Any, Callable


# --------------------------------------------------------------------------- #
# Gateway factories
# --------------------------------------------------------------------------- #

def make_ollama_gateway(
    base_url: str = "http://127.0.0.1:11434",
    timeout_s: float = 120.0,
    connect_retries: int = 1,
    backoff_s: float = 1.0,
) -> Any:
    """Production gateway factory: a real local Ollama client."""
    from ..ollama.client import OllamaClient

    return OllamaClient(
        base_url, timeout_s=timeout_s,
        connect_retries=connect_retries, backoff_s=backoff_s,
    )


def load_factory(dotted: str) -> Callable[..., Any]:
    """Resolve ``"module:attr"`` or ``"module.attr"`` to a callable."""
    if ":" in dotted:
        module_name, _, attr = dotted.partition(":")
    else:
        module_name, _, attr = dotted.rpartition(".")
    if not module_name or not attr:
        raise ValueError(f"invalid factory reference {dotted!r}")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    if not callable(obj):
        raise ValueError(f"factory {dotted!r} is not callable")
    return obj


def build_gateway(gateway_spec: dict[str, Any] | None) -> Any | None:
    """Construct the gateway named by a spec (None spec -> None)."""
    if gateway_spec is None:
        return None
    factory = load_factory(str(gateway_spec["factory"]))
    return factory(**dict(gateway_spec.get("kwargs") or {}))


# --------------------------------------------------------------------------- #
# Descendant cleanup (SIGTERM path)
# --------------------------------------------------------------------------- #

def descendant_pids(root: int) -> list[int]:
    """All descendant pids of ``root`` (deepest first), via /proc."""
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                raw = fh.read()
            # comm may contain spaces/parens: ppid follows the last ")".
            rparen = raw.rfind(b")")
            ppid = int(raw[rparen + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    stack = list(children.get(root, ()))
    while stack:
        pid = stack.pop()
        if pid == root or pid <= 1:
            continue
        found.append(pid)
        stack.extend(children.get(pid, ()))
    return found  # BFS-ish order reversed below by caller if needed


def kill_descendants(sig: int = signal.SIGKILL) -> None:
    """Kill every descendant of this process (deepest first)."""
    me = os.getpid()
    for pid in reversed(descendant_pids(me)):
        if pid == me:
            continue
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _install_signal_handlers() -> None:
    """On SIGTERM/SIGINT: kill the descendant tree, then exit immediately.

    The coordinator terminates workers with SIGTERM (then SIGKILL after a
    grace period). Sandbox launchers are session leaders in their own
    process groups, so the group kill alone would leave student processes
    running; walking /proc here — while this process is still alive to
    anchor the tree — kills them too.
    """

    def _handler(signum: int, frame: Any) -> None:
        try:
            kill_descendants(signal.SIGKILL)
        finally:
            os._exit(128 + signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


# --------------------------------------------------------------------------- #
# Context reconstruction
# --------------------------------------------------------------------------- #

def context_from_spec(spec: dict[str, Any]) -> Any:
    """Rebuild an :class:`AnalyzerContext` from its serialized spec."""
    from ..analyzers.base import AnalyzerContext
    from ..core.schemas import Evidence, MetricConfig, RuleSet, TestCase
    from ..sandbox.compiler import compile_c
    from ..sandbox.executor import SandboxExecutor
    from ..sandbox.policy import SandboxPolicy
    from ..testing.generation.budgets import BudgetTracker

    ctx_spec = spec["context"]

    executor = None
    ex_spec = ctx_spec.get("executor")
    if ex_spec is not None:
        policy = None
        if ex_spec.get("policy") is not None:
            policy = SandboxPolicy.model_validate(ex_spec["policy"])
        executor = SandboxExecutor(
            base_dir=ex_spec.get("base_dir") or None,
            use_network_namespace=bool(
                ex_spec.get("use_network_namespace", True)),
            use_landlock=bool(ex_spec.get("use_landlock", True)),
            policy=policy,
        )

    gateway = None
    gateway_error: str | None = None
    try:
        gateway = build_gateway(ctx_spec.get("gateway"))
    except Exception as exc:  # noqa: BLE001 - degrade: analyzer reports it
        gateway_error = f"{type(exc).__name__}: {exc}"

    tracker = None
    if ctx_spec.get("budget") is not None:
        tracker = BudgetTracker.from_snapshot(ctx_spec["budget"])

    compile_result = None
    if ctx_spec.get("compile_result") is not None:
        from ..sandbox.compiler import CompileResult

        compile_result = CompileResult.model_validate(
            ctx_spec["compile_result"])

    ctx = AnalyzerContext(
        source_path=ctx_spec["source_path"],
        binary_path=ctx_spec.get("binary_path"),
        sanitizer_binary_path=ctx_spec.get("sanitizer_binary_path"),
        rules=RuleSet.model_validate(ctx_spec["rules"]),
        metric=MetricConfig.model_validate(ctx_spec["metric"]),
        run_dir=ctx_spec["run_dir"],
        executor=executor,
        compiler=compile_c,
        gateway=gateway,
        evidence=[Evidence.model_validate(e)
                  for e in ctx_spec.get("evidence", [])],
        run_id=ctx_spec.get("run_id", "local"),
        test_cases=[TestCase.model_validate(tc)
                    for tc in ctx_spec.get("test_cases", [])],
        compile_result=compile_result,
        budget_tracker=tracker,
    )
    ctx._evidence_seq = int(ctx_spec.get("evidence_seq", len(ctx.evidence)))
    ctx.gateway_error = gateway_error  # diagnostic only; analyzers see None
    return ctx


# --------------------------------------------------------------------------- #
# Worker main
# --------------------------------------------------------------------------- #

def _write_envelope(result_path: Path, envelope: dict[str, Any]) -> None:
    tmp = result_path.with_suffix(result_path.suffix + ".tmp")
    tmp.write_text(json.dumps(envelope), encoding="utf-8")
    os.replace(tmp, result_path)


def run_worker(spec_path: str | Path, result_path: str | Path) -> int:
    """Execute one analyzer spec; write the result envelope. Returns exit code."""
    result_path = Path(result_path)
    _install_signal_handlers()
    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
        module_path = str(spec["module_path"])
        if ":" in module_path:
            # "module:attr" names the analyze callable directly (used by
            # test-only analyzer modules hosting several behaviors).
            analyze = load_factory(module_path)
        else:
            module = importlib.import_module(module_path)
            analyze = getattr(module, "analyze")
        ctx = context_from_spec(spec)
        initial_evidence = len(ctx.evidence)
        result = analyze(ctx)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        _write_envelope(result_path, {
            "ok": False,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(limit=8),
        })
        return 0

    from ..core.schemas import AnalyzerResult

    if not isinstance(result, AnalyzerResult):
        _write_envelope(result_path, {
            "ok": False,
            "error_type": "InvalidResultType",
            "message": (f"analyzer returned {type(result).__name__}, "
                        "not AnalyzerResult"),
            "traceback": "",
        })
        return 0

    tracker = ctx.budget_tracker
    _write_envelope(result_path, {
        "ok": True,
        "result": result.model_dump(mode="json"),
        # Only evidence minted during this invocation travels back; the
        # coordinator appends it to the run registry in metric order.
        "evidence": [ev.model_dump(mode="json")
                     for ev in ctx.evidence[initial_evidence:]],
        "budget": (tracker.snapshot()
                   if tracker is not None
                   and hasattr(tracker, "snapshot") else None),
        "gateway_error": getattr(ctx, "gateway_error", None),
    })
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m autograder.app.orchestration.worker "
              "<spec.json> <result.json>", file=sys.stderr)
        return 2
    return run_worker(args[0], args[1])


if __name__ == "__main__":
    sys.exit(main())
