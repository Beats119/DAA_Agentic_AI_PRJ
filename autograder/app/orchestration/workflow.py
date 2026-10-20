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
analyzing -> scoring -> reporting -> completed | completed_with_warnings |
failed | incomplete). Partial failure degrades to ``completed_with_warnings``;
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
import importlib
import json
import shutil
import tempfile
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    SubmissionRef,
    TestCase,
    TestStats,
)
from ..persistence.store import RunStore
from ..reporting.schemas import validate_report
from ..rules.loader import load_rules
from ..rules.versioning import hash_rules
from ..sandbox import compiler as sandbox_compiler
from ..sandbox.compiler import CompileResult, compile_c
from ..sandbox.executor import (
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
    gen = info.get("generation", {}) or {}
    return ModelRecord(
        task=task,
        model=str(gen.get("model", "unknown")),
        temperature=float(gen.get("temperature", 0.0) or 0.0),
        seed=gen.get("seed"),
        timeout_s=float(gen.get("timeout_s", 0) or 0),
        max_tokens=int(gen.get("max_tokens", 0) or 0),
        prompt_version=str(info.get("prompt_version", "unknown")),
    )


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
) -> RunResult:
    """Run one full evaluation. See the module docstring for the flow."""
    run_id = run_id or uuid.uuid4().hex
    artifacts_base = Path(base_dir or Path.cwd() / "artifacts")
    artifacts_base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"ag_eval_{run_id}_"))

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

    store = RunStore(artifacts_base)
    store.create_run(source_hash, assignment_hash, rules_hash, rules.version,
                     run_id=run_id)
    registry = EvidenceRegistry(run_id, store)

    limitations: list[str] = []
    warnings: list[str] = []
    model_records: list[ModelRecord] = []
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
            finalized = registry.finalize()
        except Exception:
            finalized = registry.all()
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
        for name, payload in (
            ("report.json", report.model_dump_json(indent=1).encode()),
            ("evidence.json", registry.to_json().encode()),
            ("feedback.json", feedback.model_dump_json(indent=1).encode()),
        ):
            try:
                store.write_artifact(run_id, name, payload)
            except Exception:
                pass
        try:
            store.set_grade(run_id, None)
        except Exception:
            pass
        try:
            _go(state)
        except Exception:
            pass
        return RunResult(
            run_id=run_id,
            report=report,
            status=state,
            artifacts_dir=_artifacts_dir(),
            feedback=feedback,
            feedback_info={"used_fallback": True, "attempts": 0,
                           "violations": (), "generation_records": []},
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
        plan = build_plan(None, rules)
        limitations.extend(plan.warnings)
        registry.append(
            "evaluation_plan", "planner",
            {"steps": [s.name for s in plan.steps],
             "analyzer_ids": list(plan.analyzer_ids),
             "warnings": list(plan.warnings)},
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
            limitations.append(
                "no LLM gateway for this run: specification extraction and "
                "test generation skipped; correctness will be inconclusive"
            )

        test_cases: list[TestCase] = []
        test_stats = {"generated": 0, "validated": 0, "executed": 0,
                      "rejected": 0, "trusted": 0}
        if spec is not None and gateway is not None:
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
            service = OracleService(
                reference_source=rules.oracle.reference_solution_path
            )
            ref_path = rules.oracle.reference_solution_path
            if ref_path:
                for s in service.strategies:
                    if isinstance(s, ReferenceSolutionOracle):
                        def _ref_compile(src: str, out_dir: str,
                                         _rules=rules, _ex=executor):
                            return compile_c(src, out_dir, _rules.compile,
                                             _ex, allowed_dir=None)

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
                        prep = s.prepare(
                            compile=lambda src: {
                                "ok": False, "binary": None,
                                "diagnostics": "unused"},
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
        jobs: list[AnalyzerJob] = []
        for metric in rules.metrics:
            if not plan.analyzer_available.get(metric.name, False):
                limitations.append(
                    f"metric {metric.name!r}: analyzer "
                    f"{metric.analyzer!r} unavailable "
                    f"({'required' if metric.required else 'optional'}); "
                    "handled per the uncertainty policy")
                continue
            module = importlib.import_module(
                ANALYZER_MODULES[metric.analyzer])
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
                analyze_fn=module.analyze,
                ctx=actx,
                timeout_s=float(metric.params.get("timeout_s", 180.0)),
            ))
        results: dict[str, AnalyzerResult] = run_analyzers(
            jobs, max_workers=max_workers)

        # Remint analyzer evidence through the central registry so ids are
        # unique per run (each analyzer context numbers from 1).
        id_map: dict[str, str] = {}
        for job in jobs:
            for ev in job.ctx.evidence:
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
                "limitations": [_remap_text(l) for l in res.limitations],
                "diagnostics": _remap_diag(res.diagnostics),
            })
        results = remapped
        for name, res in results.items():
            for w in res.warnings:
                warnings.append(f"[{name}] {w}")
            for l in res.limitations:
                limitations.append(f"[{name}] {l}")

        analyzer_statuses = {m.name: results[m.name].status
                             for m in rules.metrics if m.name in results}

        # ---------------- scoring ----------------
        _go("scoring")
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
        )
        feedback: FeedbackText = feedback_out["feedback"]
        if feedback_out["used_fallback"]:
            limitations.append(
                "feedback rendered by the deterministic fallback "
                f"({'; '.join(feedback_out['violations'])})"
                if feedback_out["violations"]
                else "feedback rendered by the deterministic fallback "
                     "(no LLM gateway)")

        # ---------------- reporting ----------------
        _go("reporting")

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

        tool_versions = {
            "sandbox": "sandbox-1.0",
            "scoring_policy": SCORING_POLICY_VERSION,
            "compiler": f"{rules.compile.compiler} {rules.compile.std}",
            "python": "3.11+",
        }
        for m in rules.metrics:
            if m.name in results:
                tool_versions[f"analyzer:{m.name}"] = \
                    results[m.name].analyzer_version

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

        manifest = RunManifest(
            run_id=run_id,
            state="reporting",  # type: ignore[arg-type]
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
        shutil.rmtree(work, ignore_errors=True)
