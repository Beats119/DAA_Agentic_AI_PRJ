"""Full evaluation workflow: the flow diagram as code.

``evaluate()`` runs one end-to-end evaluation::

    fresh run_id -> hash inputs -> validate rules -> ingest assignment
    -> extract specification (LLM) -> generate tests (LLM, bounded)
    -> oracle assessment (tiered) -> compile in sandbox
    -> analyzers (bounded parallel) -> deterministic scoring
    -> feedback (generate + verify, bounded retry, fallback)
    -> validate + persist report -> terminal state

Run states follow the Phase-2 state machine
(queued -> validating -> planning -> generating_tests -> executing ->
analyzing -> scoring -> feedback -> reporting -> completed |
completed_with_warnings | failed | incomplete). Partial failure degrades to ``completed_with_warnings``;
a required metric without evidence becomes ``incomplete`` — never a silent
``completed``.

Fresh-run vs reproduction: every input hash, tool version, model id and
prompt version is recorded in the RunManifest. Identical validated evidence
+ identical rules -> identical score (the scoring engine is deterministic).
LLM steps (spec extraction, test generation, feedback drafting) may vary
between runs within the documented acceptance bar; test generation is fresh
every run by design, never served from a cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autograder import __version__ as APP_VERSION

from ..analyzers.base import AnalyzerContext
from ..core.schemas import (
    AnalyzerResult,
    AssignmentRef,
    ExpectedProvenance,
    FailingTest,
    ModelRecord,
    Report,
    ReportMetric,
    ReproductionInfo,
    RulesRef,
    RunManifest,
    StageTiming,
    SubmissionRef,
    TestCase,
    TestStats,
)
from ..persistence.store import RunStore
from ..reporting.schemas import validate_report
from ..rules.loader import load_rules
from ..rules.versioning import hash_rules
from ..sandbox.compiler import CompileResult, compile_c
from ..sandbox.executor import (
    SANDBOX_VERSION,
    ExecLimits,
    SandboxError,
    SandboxExecutor,
    SandboxUnavailable,
)
from ..scoring.engine import POLICY_VERSION as SCORING_POLICY_VERSION
from ..scoring.engine import ScoreResult, score
from .coordinator import AnalyzerJob, run_analyzers
from .evidence import EvidenceRegistry
from .feedback import (
    FeedbackText,
    build_trusted_results,
    finalize_feedback,
)
from .planner import ANALYZER_MODULES, EvaluationPlan, build_plan

log = logging.getLogger("autograder.workflow")


class WorkflowError(RuntimeError):
    """The evaluation could not start or continue (caller-visible)."""


class _Abort(Exception):
    """Internal: jump to a terminal run state with a recorded reason."""

    def __init__(self, state: str, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason


@dataclass(frozen=True)
class RunResult:
    run_id: str
    report: Report
    status: str
    artifacts_dir: str
    feedback: FeedbackText
    feedback_info: dict[str, Any]
    plan: EvaluationPlan


def _sha256_file(path: str | Path) -> tuple[bytes, str]:
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise WorkflowError(f"cannot read {p}: {exc}") from exc
    return data, "sha256:" + hashlib.sha256(data).hexdigest()


def _model_record(task: str, info: dict[str, Any]) -> ModelRecord:
    """Build a ModelRecord from a generation record dict (D20).

    Two shapes exist in the pipeline: *wrapped* records
    (``{"generation": {...}, "prompt_version": ...}`` — specification
    extraction and feedback) and *flat* GenerationRecord dumps (test
    generation). Both must yield the real model id and prompt version;
    the old reader only understood the wrapped shape, so test-generation
    provenance silently degraded to ``"unknown"``.
    """
    gen = info.get("generation") or (info if "model" in info else {}) or {}
    return ModelRecord(
        task=task,
        model=str(gen.get("model") or info.get("model") or "unknown"),
        temperature=float(gen.get("temperature", 0.0) or 0.0),
        seed=gen.get("seed"),
        timeout_s=float(gen.get("timeout_s", 0) or 0),
        max_tokens=int(gen.get("max_tokens", 0) or 0),
        prompt_version=str(
            info.get("prompt_version")
            or gen.get("prompt_version")
            or "unknown"
        ),
    )


def _no_llm_record(task: str) -> ModelRecord:
    """Honest provenance for a task that ran without any LLM (D20).

    Recorded when no gateway exists for the run: the model is stated as
    ``none (llm unavailable)`` — never ``"unknown"``, which would imply a
    model ran but was not recorded.
    """
    return ModelRecord(
        task=task,
        model="none (llm unavailable)",
        temperature=0.0,
        seed=None,
        timeout_s=0.0,
        max_tokens=0,
        prompt_version="none",
    )


def _gateway_spec(gateway: Any | None) -> dict[str, Any] | None:
    """Name the factory an analyzer worker uses to rebuild the gateway.

    Production gateways are Ollama clients, rebuilt in the worker from
    the live client's own settings. Any other object is referenced by
    its class's dotted path (a test seam for test doubles — see
    ``orchestration.worker``); it must be constructible with no args.
    """
    if gateway is None:
        return None
    from ..ollama.client import OllamaClient

    if isinstance(gateway, OllamaClient):
        return {
            "factory": ("autograder.app.orchestration.worker:"
                        "make_ollama_gateway"),
            "kwargs": {
                "base_url": gateway.base_url,
                "timeout_s": gateway.timeout_s,
                "connect_retries": gateway.connect_retries,
                "backoff_s": gateway.backoff_s,
            },
        }
    cls = type(gateway)
    return {"factory": f"{cls.__module__}:{cls.__qualname__}", "kwargs": {}}


def _tool_versions(
    rules: Any,
    analyzer_results: dict[str, AnalyzerResult] | None = None,
    compiler_label: str | None = None,
) -> dict[str, str]:
    """Tool/version provenance from constants and probed values (D20).

    No hand-written version strings: the app version, sandbox version
    and scoring policy come from their defining constants, the Python
    version is the interpreter's own, and the compiler label is the one
    the sandbox actually ran (falling back to the configured name
    before compilation has happened).
    """
    versions = {
        "app": APP_VERSION,
        "sandbox": SANDBOX_VERSION,
        "scoring_policy": SCORING_POLICY_VERSION,
        "python": platform.python_version(),
        "compiler": compiler_label or str(rules.compile.compiler),
        "c_standard": str(rules.compile.std),
    }
    for name, res in (analyzer_results or {}).items():
        versions[f"analyzer:{name}"] = res.analyzer_version
    return versions


def _sanitize_paths(text: str, work: Path) -> str:
    return text.replace(str(work), "<run_dir>")


def evaluate(
    assignment_path: str | Path,
    source_path: str | Path,
    rules_path: str | Path,
    *,
    gateway: Any | None = None,
    run_id: str | None = None,
    base_dir: str | Path | None = None,
    max_workers: int = 4,
    evaluation_timeout_s: float = 1800.0,
    store: RunStore | None = None,
    feedback_llm_check: bool | None = None,
) -> RunResult:
    """Run one full evaluation. See the module docstring for the flow.

    ``feedback_llm_check`` enables the OPTIONAL supplementary LLM audit
    of feedback drafts (default off; when None it is resolved from the
    ``AUTOGRADER_FEEDBACK_LLM_CHECK`` env var). The deterministic
    verifier remains authoritative either way.
    """
    if feedback_llm_check is None:
        feedback_llm_check = os.environ.get(
            "AUTOGRADER_FEEDBACK_LLM_CHECK", ""
        ).strip().lower() in ("1", "true", "yes", "on")
    run_id = run_id or uuid.uuid4().hex
    artifacts_base = Path(base_dir or Path.cwd() / "artifacts")
    artifacts_base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"ag_eval_{run_id}_"))

    # --- terminal step logging -------------------------------------------
    # One line when a step starts, one when it ends with its elapsed
    # time, so an operator watching the terminal can see exactly where
    # an evaluation is spending its time (usually: waiting on the LLM).
    run_t0 = time.monotonic()
    _step_name: str | None = None
    _step_t0 = 0.0
    _step_started_at: datetime | None = None
    stage_timings: list[StageTiming] = []

    def _step_done() -> None:
        nonlocal _step_name
        if _step_name is not None:
            duration_s = time.monotonic() - _step_t0
            log.info("run %s step %s done in %.1fs",
                     run_id[:8], _step_name, duration_s)
            if _step_started_at is not None:
                stage_timings.append(StageTiming(
                    stage=_step_name, started_at=_step_started_at,
                    duration_s=duration_s))
            _step_name = None

    def _step(name: str) -> None:
        nonlocal _step_name, _step_t0, _step_started_at
        _step_done()
        _step_name = name
        _step_t0 = time.monotonic()
        _step_started_at = datetime.now(timezone.utc)
        log.info("run %s step %s ...", run_id[:8], name)

    def _finished(status: str, detail: str = "") -> None:
        suffix = f" ({detail})" if detail else ""
        log.info("run %s finished: %s in %.1fs%s",
                 run_id[:8], status, time.monotonic() - run_t0, suffix)

    _step("validating")

    # --- inputs: read + hash + validate rules (fail fast, before any record)
    assignment_bytes, assignment_hash = _sha256_file(assignment_path)
    source_bytes, source_hash = _sha256_file(source_path)
    try:
        rules = load_rules(rules_path)
    except Exception as exc:  # RulesError and friends
        shutil.rmtree(work, ignore_errors=True)
        raise WorkflowError(f"invalid rules config {rules_path}: {exc}") from exc
    rh = hash_rules(rules)
    rules_hash = rh if rh.startswith("sha256:") else "sha256:" + rh

    # Share the caller's store when one is supplied (the API passes its
    # own, so a process never holds two RunStore instances on one DB —
    # D26); otherwise open one rooted at the artifacts base.
    store = store if store is not None else RunStore(artifacts_base)
    store.create_run(source_hash, assignment_hash, rules_hash, rules.version,
                     run_id=run_id)
    registry = EvidenceRegistry(run_id, store)

    limitations: list[str] = []
    warnings: list[str] = []
    model_records: list[ModelRecord] = []
    if gateway is None:
        # Honest provenance (D20): no model ran for the LLM tasks.
        model_records.extend(
            _no_llm_record(task)
            for task in ("specification", "test_generation", "feedback")
        )
    plan: EvaluationPlan | None = None

    def _go(state: str) -> None:
        store.transition_run(run_id, state)

    def _artifacts_dir() -> str:
        return str(artifacts_base / "runs" / run_id)

    def _abort(state: str, reason: str) -> RunResult:
        raise _Abort(state, reason)

    # ------------------------------------------------------------------ #
    # Terminal handlers (failed / incomplete): build a minimal honest
    # report, persist it, and land the run in the terminal state.
    # ------------------------------------------------------------------ #
    def _finish_aborted(state: str, reason: str) -> RunResult:
        limitations.append(reason)
        try:
            registry.finalize()
        except Exception:
            pass  # report below is built from registry.all() either way
        feedback = FeedbackText(
            summary=f"Evaluation ended as {state}: {reason}",
            per_failure=[],
            recommendations=[],
            uncertainty_notes=list(limitations),
        )
        report = Report(
            run_id=run_id,
            submission=SubmissionRef(
                id=Path(source_path).stem, source_hash=source_hash
            ),
            assignment=AssignmentRef(
                id=rules.assignment_ref, spec_hash=assignment_hash
            ),
            rules=RulesRef(version=rules.version, hash=rules_hash),
            grade=None,
            status=state,  # type: ignore[arg-type]
            limitations=list(limitations),
            uncertainty=list(limitations),
            scoring_policy_version=SCORING_POLICY_VERSION,
            feedback=feedback.model_dump(mode="json"),
            reproduction=ReproductionInfo(
                run_id=run_id,
                rules_hash=rules_hash,
                scoring_policy_version=SCORING_POLICY_VERSION,
                notes="Aborted run: no grade computed.",
            ),
        )
        validate_report(report)
        # Close whichever step was open when the run aborted so its
        # partial timing is recorded in the manifest.
        _step_done()
        # The manifest is written at finalize with the run's TERMINAL
        # state and finish time (D20) — never a mid-pipeline snapshot.
        manifest = RunManifest(
            run_id=run_id,
            state=state,  # type: ignore[arg-type]
            finished_at=datetime.now(timezone.utc),
            submission_hash=source_hash,
            assignment_hash=assignment_hash,
            rules_hash=rules_hash,
            rules_version=rules.version,
            stage_timings=stage_timings,
            tool_versions=_tool_versions(rules),
            model_settings={
                r.task: {"model": r.model, "temperature": r.temperature,
                         "seed": r.seed, "prompt_version": r.prompt_version}
                for r in model_records
            },
            prompt_versions={r.task: r.prompt_version
                             for r in model_records},
            scoring_policy_version=SCORING_POLICY_VERSION,
        )
        # Artifact-write failures are LOGGED (surfaced in the run result),
        # never silently swallowed (D20).
        artifact_errors: list[str] = []
        for name, payload in (
            ("report.json", report.model_dump_json(indent=1).encode()),
            ("evidence.json", registry.to_json().encode()),
            ("feedback.json", feedback.model_dump_json(indent=1).encode()),
            ("manifest.json",
             manifest.model_dump_json(indent=1).encode()),
        ):
            try:
                store.write_artifact(run_id, name, payload)
            except Exception as exc:  # noqa: BLE001 - logged below
                artifact_errors.append(f"{name}: {exc}")
        try:
            store.set_grade(run_id, None)
        except Exception:
            pass
        try:
            _go(state)
        except Exception:
            pass
        _finished(state, reason[:160])
        return RunResult(
            run_id=run_id,
            report=report,
            status=state,
            artifacts_dir=_artifacts_dir(),
            feedback=feedback,
            feedback_info={"used_fallback": True, "attempts": 0,
                           "violations": (), "generation_records": [],
                           "artifact_write_errors": artifact_errors},
            plan=plan or build_plan(None, rules),
        )

    # ------------------------------------------------------------------ #
    try:
        # ---------------- validating ----------------
        _go("validating")
        from ..ingestion.pdf import IngestionError, ingest_assignment

        try:
            doc = ingest_assignment(str(assignment_path))
        except IngestionError as exc:
            _abort("failed", f"assignment ingestion failed: {exc}")
        registry.append(
            "assignment_ingested", "ingestion",
            {"filename": doc.filename, "sha256": doc.sha256,
             "pages": len(doc.pages), "chars": len(doc.text)},
            affects_grade=False,
        )

        # ---------------- planning ----------------
        _go("planning")
        _step("planning")
        # The plan is necessarily built before spec extraction (spec=None).
        # Its analyzer-availability warnings apply immediately; the
        # spec-degradation warning does NOT — it is surfaced only if the
        # spec turns out to be genuinely unavailable (D16), and the plan
        # is refreshed with the real spec after extraction below.
        plan = build_plan(None, rules)
        limitations.extend(plan.warnings)
        registry.append(
            "evaluation_plan", "planner",
            {"steps": [s.name for s in plan.steps],
             "analyzer_ids": list(plan.analyzer_ids),
             "warnings": list(plan.warnings),
             "spec_warning_deferred": plan.spec_warning},
            affects_grade=False,
        )

        # Work copy of the submission (originals are never touched).
        work_source = work / f"{Path(source_path).stem}.c"
        shutil.copy2(str(source_path), work_source)

        # Sandbox executor: created early — the oracle needs it too.
        # Fail closed: no isolation, no untrusted execution, no run.
        executor = SandboxExecutor(base_dir=work / "sandbox")
        try:
            executor.ensure_available()
        except SandboxUnavailable as exc:
            _abort("failed", f"sandbox isolation unavailable: {exc} "
                             "(fail-closed: refusing to execute untrusted code)")
        registry.append(
            "sandbox_capabilities", "sandbox",
            executor.capabilities.model_dump(mode="json"),
            affects_grade=False,
        )

        # ---------------- generating_tests ----------------
        _go("generating_tests")
        from ..specification.agent import (
            SpecificationError,
            extract_specification,
        )
        from ..testing.generation.agent import generate_test_suite
        from ..testing.generation.budgets import BudgetTracker
        from ..testing.oracle.service import OracleService
        from ..testing.oracle.strategies import (
            OracleContext,
            ReferenceSolutionOracle,
        )

        # One per-run budget counter (D10), shared by test generation
        # (tokens), the oracle subsystem and the correctness analyzer
        # (sandbox executions) so the configured budgets bound the
        # evaluation as a whole, not each phase in isolation.
        budget_tracker = BudgetTracker.from_policy(rules.test_generation)

        spec = None
        if gateway is not None:
            # Best-effort warm-up: a cold model load (~90s) must not
            # land inside the specification call's idle window. Only
            # gateways that offer warmup get it (test doubles without
            # the method are skipped silently); a warm-up failure is
            # logged and the run continues — the task calls themselves
            # surface any real error.
            warmup = getattr(gateway, "warmup", None)
            if callable(warmup):
                try:
                    from ..ollama.models import model_cfg_for

                    warm_model = str(model_cfg_for("specification")["model"])
                    warm_s = warmup(warm_model)
                    log.info("run %s warm-up: model %s ready in %.1fs",
                             run_id[:8], warm_model, warm_s)
                except Exception as exc:  # noqa: BLE001 - best-effort only
                    log.warning(
                        "run %s warm-up failed: %s — continuing; "
                        "task calls will surface errors", run_id[:8], exc)
            _step("specification")
            try:
                spec, spec_record = extract_specification(doc, gateway)
                model_records.append(_model_record("specification", spec_record))
                registry.append(
                    "specification_extracted", "specification",
                    {"requirements": len(spec.requirements),
                     "examples": len(spec.examples),
                     "ambiguities": len(spec.ambiguities),
                     "post_validation_notes": spec_record.get(
                         "post_validation_notes", [])},
                    affects_grade=False,
                )
            except SpecificationError as exc:
                limitations.append(
                    f"specification extraction failed ({exc}); "
                    "test generation impossible, correctness will be inconclusive"
                )
        else:
            _step("generating_tests")
            log.info("run %s no LLM gateway: specification extraction and "
                     "test generation skipped", run_id[:8])
            limitations.append(
                "no LLM gateway for this run: specification extraction and "
                "test generation skipped; correctness will be inconclusive"
            )

        # Refresh the plan with the spec as it actually turned out (D16):
        # only a genuinely missing spec earns the plan's spec warning.
        plan = build_plan(spec, rules)
        if spec is None and plan.spec_warning:
            limitations.append(plan.spec_warning)

        test_cases: list[TestCase] = []
        test_stats = {"generated": 0, "validated": 0, "executed": 0,
                      "rejected": 0, "trusted": 0}
        if spec is not None and gateway is not None:
            _step("generating_tests")
            suite = generate_test_suite(
                spec, rules, gateway, budget_tracker=budget_tracker)
            limitations.extend(suite.get("limitations", []))
            for rec in suite["generation_records"]:
                model_records.append(_model_record("test_generation", rec))
            candidates = suite["candidates"]
            quarantined = suite["quarantined"]
            test_stats["generated"] = len(candidates) + len(quarantined)
            test_stats["validated"] = len(candidates)
            test_stats["rejected"] = len(quarantined)
            registry.append(
                "tests_generated", "test_generation",
                {"accepted": len(candidates),
                 "quarantined": quarantined,
                 "rounds_used": suite["rounds_used"],
                 "uncovered_requirements": suite["uncovered_requirements"]},
                affects_grade=False,
            )

            # --- tiered oracle assessment ---
            _step("oracle_assessment")
            service = OracleService(
                reference_source=rules.oracle.reference_solution_path
            )
            ref_path = rules.oracle.reference_solution_path
            if ref_path:
                for s in service.strategies:
                    if isinstance(s, ReferenceSolutionOracle):
                        def _ref_compile(src: str, out_dir: str,
                                         _rules=rules, _ex=executor):
                            # The traversal guard stays ON: the reference
                            # source may only be read from its own
                            # directory (it is instructor-configured).
                            return compile_c(
                                src, out_dir, _rules.compile, _ex,
                                allowed_dir=str(
                                    Path(src).resolve().parent))

                        s.set_executor(
                            executor, _ref_compile,
                            comparison_policy=rules.comparison_policy,
                            work_dir=str(work / "oracle_ref"),
                            limits=ExecLimits(
                                wall_time_s=rules.limits.time_limit_s,
                                memory_mb=rules.limits.memory_limit_mb,
                            ),
                        )
                        s.set_budget_tracker(budget_tracker)

                        def _ref_prepare_compile(
                                src: str, _cc=_ref_compile):
                            # prepare()'s expected adapter shape. (With a
                            # sandbox executor wired, prepare uses its own
                            # sandbox adapter; this one is truthful if used.)
                            res = _cc(src, str(work / "oracle_ref"))
                            return {"ok": bool(res.ok),
                                    "binary": res.binary_path,
                                    "diagnostics": res.raw_stderr}

                        prep = s.prepare(
                            compile=_ref_prepare_compile,
                            spec=spec,
                        )
                        prep_record = {k: v for k, v in prep.items()
                                       if k != "binary"}
                        registry.append(
                            "oracle_reference_prepared", "oracle", prep_record,
                            affects_grade=False,
                        )
            instructor_expected: dict[str, str] = {}
            if rules.oracle.expected_outputs_path:
                try:
                    raw = json.loads(
                        Path(rules.oracle.expected_outputs_path).read_text(
                            encoding="utf-8"))
                    if isinstance(raw, dict):
                        instructor_expected = {
                            str(k): str(v) for k, v in raw.items()}
                except Exception as exc:
                    limitations.append(
                        f"could not load instructor expected outputs "
                        f"({exc}); continuing without them")
            ctx = OracleContext(
                gateway=gateway, instructor_expected=instructor_expected,
                budget_tracker=budget_tracker,
                rules_version=rules.version, rules_hash=rules_hash)
            test_cases, status_counts, chains = service.assess_all(
                candidates, spec, rules, ctx)
            test_stats["trusted"] = int(status_counts.get("trusted", 0))
            registry.append(
                "oracle_assessment", "oracle",
                {"status_counts": dict(status_counts),
                 "chains": [
                     {"test_id": c["test_id"],
                      "verdicts": [(v["strategy"], v["tier"], v["status"])
                                   for v in c["chain"]]}
                     for c in chains]},
                affects_grade=False,
            )
            if test_stats["trusted"] == 0 and test_stats["validated"] > 0:
                limitations.append(
                    "no generated test achieved a trusted oracle verdict; "
                    "correctness cannot be confirmed from test evidence")

        # ---------------- executing ----------------
        _go("executing")
        _step("compiling")

        def _compile_fn(src: str, out_dir: str) -> CompileResult:
            return compile_c(src, out_dir, rules.compile, executor,
                             allowed_dir=work)

        try:
            compile_result: CompileResult | None = _compile_fn(
                str(work_source), str(work / "bin"))
        except (SandboxError, SandboxUnavailable) as exc:
            _abort("failed", f"compilation could not run in the sandbox: {exc}")
        compile_record = compile_result.model_dump(mode="json")
        # Never leak server paths into persisted records.
        compile_record = json.loads(
            _sanitize_paths(json.dumps(compile_record), work))
        compile_record.pop("binary_path", None)
        registry.append("compile_result", "compiler", compile_record,
                        affects_grade=False)
        binary_path = (compile_result.binary_path
                       if compile_result.ok else None)
        if not compile_result.ok:
            limitations.append(
                "submission did not compile; execution-based metrics "
                "cannot be measured")

        # ---------------- analyzing ----------------
        _go("analyzing")
        _step("analyzing")
        gateway_spec = _gateway_spec(gateway)
        jobs: list[AnalyzerJob] = []
        for metric in rules.metrics:
            if not plan.analyzer_available.get(metric.name, False):
                limitations.append(
                    f"metric {metric.name!r}: analyzer "
                    f"{metric.analyzer!r} unavailable "
                    f"({'required' if metric.required else 'optional'}); "
                    "handled per the uncertainty policy")
                continue
            actx = AnalyzerContext(
                source_path=str(work_source),
                binary_path=binary_path,
                sanitizer_binary_path=None,
                rules=rules,
                metric=metric,
                run_dir=str(work),
                executor=executor,
                compiler=compile_c,
                gateway=gateway,
                run_id=run_id,
                test_cases=test_cases,
                compile_result=(compile_result
                                if metric.analyzer == "compiler" else None),
                budget_tracker=budget_tracker,
            )
            jobs.append(AnalyzerJob(
                metric_name=metric.name,
                analyzer_id=metric.analyzer,
                module_path=ANALYZER_MODULES[metric.analyzer],
                ctx=actx,
                timeout_s=float(metric.params.get("timeout_s", 180.0)),
                gateway_spec=gateway_spec,
            ))
        # Analyzers run in worker subprocesses with hard per-analyzer and
        # overall deadlines (D5): a hung analyzer is terminated, never
        # waited on forever.
        budget_seed = budget_tracker.snapshot()
        outcomes = run_analyzers(
            jobs, max_workers=max_workers,
            evaluation_timeout_s=evaluation_timeout_s)
        results: dict[str, AnalyzerResult] = {
            name: outcome.result for name, outcome in outcomes.items()
        }
        # Reconcile worker-side budget consumption into the run tracker:
        # each worker was seeded from the same snapshot, so the sum of
        # per-worker deltas is the analyzer phase's total consumption.
        for outcome in outcomes.values():
            if outcome.budget:
                budget_tracker.add_tokens(max(
                    0, int(outcome.budget.get("tokens_used", 0))
                    - budget_seed["tokens_used"]))
                budget_tracker.add_execs(max(
                    0, int(outcome.budget.get("execs_used", 0))
                    - budget_seed["execs_used"]))

        # Remint analyzer evidence through the central registry so ids are
        # unique per run (each worker context numbers from 1). Iterating
        # jobs — rules order, not completion order — keeps evidence
        # numbering deterministic.
        id_map: dict[str, str] = {}
        for job in jobs:
            for ev in outcomes[job.metric_name].evidence:
                new_id = registry.append(
                    ev.kind, ev.analyzer, ev.data,
                    severity=ev.severity, confidence=ev.confidence,
                    affects_grade=ev.affects_grade)
                id_map[ev.id] = new_id

        def _remap_text(text: str) -> str:
            for old, new in id_map.items():
                text = text.replace(old, new)
            return _sanitize_paths(text, work)

        def _remap_diag(value: Any) -> Any:
            if isinstance(value, str):
                return _remap_text(value)
            if isinstance(value, list):
                return [_remap_diag(v) for v in value]
            if isinstance(value, dict):
                return {k: _remap_diag(v) for k, v in value.items()}
            return value

        remapped: dict[str, AnalyzerResult] = {}
        for name, res in results.items():
            # AnalyzerResult is frozen: remap via model_copy, never mutation.
            remapped[name] = res.model_copy(update={
                "evidence_refs": [id_map.get(e, e)
                                  for e in res.evidence_refs],
                "findings": [_remap_text(f) for f in res.findings],
                "warnings": [_remap_text(w) for w in res.warnings],
                "limitations": [_remap_text(lim) for lim in res.limitations],
                "diagnostics": _remap_diag(res.diagnostics),
            })
        results = remapped
        for name, res in results.items():
            for w in res.warnings:
                warnings.append(f"[{name}] {w}")
            for lim in res.limitations:
                limitations.append(f"[{name}] {lim}")

        analyzer_statuses = {m.name: results[m.name].status
                             for m in rules.metrics if m.name in results}

        # ---------------- scoring ----------------
        _go("scoring")
        _step("scoring")
        finalized_evidence = registry.finalize()
        score_result: ScoreResult = score(
            [results[m.name] for m in rules.metrics if m.name in results],
            rules,
        )
        warnings.extend(score_result.warnings)
        # Belt and braces: any failed analyzer forces at least warnings.
        report_status = {
            "complete": "completed",
            "completed_with_warnings": "completed_with_warnings",
            "incomplete": "incomplete",
        }[score_result.status]
        if report_status == "completed" and any(
                r.status == "failed" for r in results.values()):
            report_status = "completed_with_warnings"
            warnings.append(
                "an analyzer failed; downgraded to completed_with_warnings")
        grade_value = (float(score_result.grade)
                       if score_result.grade is not None else None)

        # Trusted test outcomes, rebuilt from finalized evidence (remapped ids).
        trusted_results, failing_tests = build_trusted_results(registry)
        test_stats["executed"] = len(trusted_results)
        passing_tests = sum(1 for r in trusted_results.values()
                            if r["passed"])

        # ---------------- feedback ----------------
        _go("feedback")
        _step("feedback")
        feedback_out = finalize_feedback(
            evidence_json=registry.to_json(),
            gateway=gateway,
            registry=registry,
            trusted_results=trusted_results,
            grade=grade_value,  # read-only scalar, for the no-restatement check
            analyzer_statuses=analyzer_statuses,
            failing_tests=failing_tests,
            analyzer_notes=[
                f"[{m.name}] {f}"
                for m in rules.metrics if m.name in results
                for f in results[m.name].findings[:3]
            ],
            uncertainty_notes=list(limitations),
            test_stats=test_stats,
            run_status=report_status,
            llm_check=feedback_llm_check,
            # D21: the verifier checks cited source lines against the
            # real submission and metric-score claims against the scores
            # the engine just computed (both read-only here).
            source_lines=len(
                source_bytes.decode("utf-8", errors="replace").splitlines()),
            metric_scores={
                m.name: (float(m.score) if m.score is not None else None)
                for m in score_result.per_metric
            },
        )
        feedback: FeedbackText = feedback_out["feedback"]
        for rec in feedback_out.get("generation_records", []):
            model_records.append(_model_record("feedback", rec))
        if feedback_out["used_fallback"]:
            limitations.append(
                "feedback rendered by the deterministic fallback "
                f"({'; '.join(feedback_out['violations'])})"
                if feedback_out["violations"]
                else "feedback rendered by the deterministic fallback "
                     "(no LLM gateway)")

        # ---------------- reporting ----------------
        _go("reporting")
        _step("reporting")

        # The report's uncertainty section must ALWAYS carry our caveats,
        # regardless of what the LLM put in its own uncertainty notes.
        # (Analyzer/engine warnings live here: the Report schema has no
        # separate warnings field, and these are exactly the caveats a
        # reader must see.)
        merged_uncertainty: list[str] = []
        for item in (list(feedback.uncertainty_notes) + limitations
                     + warnings):
            if item not in merged_uncertainty:
                merged_uncertainty.append(item)
        metrics_report = []
        for mc in rules.metrics:
            ms = next((m for m in score_result.per_metric
                       if m.name == mc.name), None)
            res = results.get(mc.name)
            # Use the scoring engine's (possibly renormalized) weight so the
            # report reconciles: contribution == 100 * weight * score.
            # Policy 1.1: expose BOTH the configured weight (rules) and the
            # effective weight (engine) explicitly.
            metrics_report.append(ReportMetric(
                name=mc.name,
                analyzer=mc.analyzer,
                weight=float(ms.effective_weight) if ms else float(mc.weight),
                configured_weight=float(mc.weight),
                effective_weight=(float(ms.effective_weight)
                                  if ms else float(mc.weight)),
                score=float(ms.score) if ms and ms.score is not None else None,
                contribution=float(ms.contribution) if ms else 0.0,
                status=res.status if res else "missing",
            ))

        def _measurements(name: str) -> dict[str, Any] | None:
            res = results.get(name)
            if res is None or res.status != "completed":
                return None
            return dict(res.measurements or {})

        failing_report = []
        for t in failing_tests:
            try:
                prov = ExpectedProvenance(**(t.get("oracle_provenance") or {}))
            except Exception:
                prov = ExpectedProvenance(
                    tier="unknown", method="unknown",
                    detail="provenance unavailable")
            failing_report.append(FailingTest(
                test_id=str(t.get("test_id")),
                input=str(t.get("input")),
                expected=str(t.get("expected")),
                expected_provenance=prov,
                actual=(None if t.get("actual") is None
                        else str(t.get("actual"))),
                note=f"oracle: {prov.tier}/{prov.method}",
            ))

        compiler_res = results.get("compiler")
        compiler_diagnostics: list[str] = []
        if compiler_res is not None:
            for f in compiler_res.findings:
                compiler_diagnostics.append(f)
            raw = (compiler_res.diagnostics or {}).get("raw_stderr")
            if raw:
                compiler_diagnostics.append(str(raw)[-2000:])

        tool_versions = _tool_versions(
            rules,
            analyzer_results=results,
            compiler_label=(compile_result.compiler
                            if compile_result is not None else None),
        )

        report = Report(
            run_id=run_id,
            submission=SubmissionRef(
                id=Path(source_path).stem, source_hash=source_hash),
            assignment=AssignmentRef(
                id=rules.assignment_ref, spec_hash=assignment_hash),
            rules=RulesRef(version=rules.version, hash=rules_hash),
            grade=grade_value,
            status=report_status,  # type: ignore[arg-type]
            evidence_coverage={
                "evidence_records": len(finalized_evidence),
                "grade_affecting": sum(
                    1 for e in finalized_evidence if e.affects_grade),
                "metrics_completed": sum(
                    1 for r in results.values()
                    if r.status == "completed"),
                "metrics_total": len(rules.metrics),
            },
            metrics=metrics_report,
            analyzer_statuses=analyzer_statuses,
            test_stats=TestStats(**test_stats),
            passing_tests=passing_tests,
            failing_tests=failing_report,
            compiler_diagnostics=[
                _sanitize_paths(d, work) for d in compiler_diagnostics],
            runtime_measurements=_measurements("correctness") or {},
            memory_safety=_measurements("memory_safety"),
            complexity=_measurements("complexity"),
            similarity=_measurements("similarity"),
            suggestions=list(feedback.recommendations),
            uncertainty=merged_uncertainty,
            limitations=list(limitations),
            tool_versions=tool_versions,
            models=model_records,
            scoring_policy_version=SCORING_POLICY_VERSION,
            feedback=feedback.model_dump(mode="json"),
            reproduction=ReproductionInfo(
                run_id=run_id,
                rules_hash=rules_hash,
                scoring_policy_version=SCORING_POLICY_VERSION,
                notes=(
                    "Deterministic scoring: identical (assignment, source, "
                    "rules, tool versions, model ids, prompt versions) and "
                    "identical validated evidence -> identical score. LLM "
                    "steps (spec extraction, test generation, feedback "
                    "drafting) may vary between runs within the +/-7 "
                    "grade-point acceptance bar; test generation is fresh "
                    "every run by design, never served from a cache."
                ),
            ),
        )
        validate_report(report, rules)

        # Close the reporting step so its timing is recorded before the
        # manifest (which carries the timings) is constructed.
        _step_done()
        manifest = RunManifest(
            run_id=run_id,
            # Written at finalize (below, just before the terminal state
            # transition) with the run's terminal state + finish time (D20).
            state=report_status,  # type: ignore[arg-type]
            finished_at=datetime.now(timezone.utc),
            submission_hash=source_hash,
            assignment_hash=assignment_hash,
            rules_hash=rules_hash,
            rules_version=rules.version,
            tool_versions=tool_versions,
            model_settings={
                r.task: {"model": r.model, "temperature": r.temperature,
                         "seed": r.seed, "prompt_version": r.prompt_version}
                for r in model_records
            },
            prompt_versions={r.task: r.prompt_version
                             for r in model_records},
            scoring_policy_version=SCORING_POLICY_VERSION,
            stage_timings=stage_timings,
        )

        # Persist artifacts BEFORE the terminal transition (the store
        # refuses writes to finalized runs).
        store.write_artifact(
            run_id, "report.json",
            report.model_dump_json(indent=1).encode())
        store.write_artifact(
            run_id, "evidence.json", registry.to_json().encode())
        store.write_artifact(
            run_id, "manifest.json",
            manifest.model_dump_json(indent=1).encode())
        store.write_artifact(
            run_id, "feedback.json",
            feedback.model_dump_json(indent=1).encode())
        store.write_artifact(run_id, "source.c", source_bytes)
        try:
            store.write_artifact(run_id, "assignment.bin", assignment_bytes)
        except Exception:
            pass
        store.set_grade(
            run_id, None if grade_value is None else str(grade_value))
        _go(report_status)
        _step_done()
        _finished(report_status,
                  f"grade {grade_value:.1f}" if grade_value is not None
                  else "no grade (insufficient trusted evidence)")

        return RunResult(
            run_id=run_id,
            report=report,
            status=report_status,
            artifacts_dir=_artifacts_dir(),
            feedback=feedback,
            feedback_info={k: v for k, v in feedback_out.items()
                           if k != "feedback"},
            plan=plan,
        )

    except _Abort as ab:
        return _finish_aborted(ab.state, ab.reason)
    except Exception as exc:  # last resort: never leak an untracked run
        tb = traceback.format_exc(limit=8)
        try:
            return _finish_aborted(
                "failed",
                f"unexpected workflow error: {type(exc).__name__}: {exc}")
        except Exception:
            # Even the abort path failed; the run record keeps the truth.
            raise WorkflowError(
                f"evaluation failed irrecoverably: {exc}\n{tb}") from exc
    finally:
        _step_done()
        shutil.rmtree(work, ignore_errors=True)
