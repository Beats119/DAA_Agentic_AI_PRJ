"""Tiered oracle strategies.

Every strategy answers one question: *how was this test's expected output
established?* The answer is an :class:`OracleVerdict` with a status and full
provenance. Strategies are tried in tier-preference order by
:class:`OracleService`; the first non-``inconclusive`` verdict wins.

INVARIANT (also encoded as a unit test): no code path may promote a Tier-3
LLM agreement to ``trusted``. Agreement between model calls is supporting
evidence only — the strongest Tier-3 outcome is ``provisional``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from pydantic import Field

from ...core.schemas import (
    ExpectedProvenance,
    OracleVerdict,
    Specification,
    StrictModel,
    TestCase,
)

TIER1 = "tier1_reference"
TIER2 = "tier2_independent"
TIER3 = "tier3_llm"
INSTRUCTOR = "instructor"


def normalize_ws(text: str) -> str:
    """Canonical form for comparing example inputs/outputs."""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


class SecondPathAnswer(StrictModel):
    """Wire shape for the Tier-3 independent reasoning path."""

    reasoning: str = Field(min_length=1)
    expected_output: str


class OracleStrategy(ABC):
    """One verifiable way of establishing an expected output."""

    name: str = "base"
    tier: str = TIER3

    @abstractmethod
    def assess(
        self, test_case: TestCase, spec: Specification, ctx: "OracleContext"
    ) -> OracleVerdict:
        """Return a verdict. ``inconclusive`` means "try the next strategy"."""


class OracleContext(StrictModel):
    """Inputs a strategy may need. All optional; strategies degrade honestly."""

    model_config = {"arbitrary_types_allowed": True}

    instructor_expected: dict[str, str] = Field(default_factory=dict)
    reference_executor: Any = None  # Callable[[str], str|None]; wired in Phase 4
    gateway: Any = None  # OllamaClient; needed only by Tier-3


# ---------------------------------------------------------------------------
# Tier 1 — instructor evidence
# ---------------------------------------------------------------------------


class InstructorExpectedOracle(OracleStrategy):
    """Instructor-supplied expected outputs (from the rules config).

    Tier 1: the instructor is authoritative for their own assignment.
    Provenance records exactly that — no LLM involved.
    """

    name = "instructor_expected"
    tier = INSTRUCTOR

    def assess(
        self, test_case: TestCase, spec: Specification, ctx: OracleContext
    ) -> OracleVerdict:
        key = normalize_ws(test_case.input)
        for raw_in, raw_out in ctx.instructor_expected.items():
            if normalize_ws(raw_in) == key:
                return OracleVerdict(
                    status="trusted",
                    provenance=ExpectedProvenance(
                        tier="instructor",
                        method="instructor_provided",
                        detail=(
                            "expected output supplied by the instructor in the "
                            "grading rules for this exact input"
                        ),
                    ),
                    evidence={"matched_input": raw_in},
                )
        return OracleVerdict(
            status="inconclusive",
            provenance=ExpectedProvenance(
                tier="instructor",
                method="instructor_provided",
                detail="no instructor expected output for this input",
            ),
        )


class ReferenceSolutionOracle(OracleStrategy):
    """Tier 1 — validated reference implementation.

    :meth:`prepare` compiles the reference (via a caller-supplied compiler
    callable, or the sandboxed one after :meth:`set_executor`) and validates
    it against the spec's worked examples. A reference that fails its own
    validation (doesn't reproduce the spec examples) is NEVER trusted — it
    is downgraded, per the clarifications.

    :meth:`assess` runs the reference in the sandbox when an executor is
    wired via :meth:`set_executor`; otherwise it falls back to
    ``ctx.reference_executor`` and, when neither exists, honestly returns
    ``inconclusive`` instead of fabricating results.
    """

    name = "reference_solution"
    tier = TIER1

    def __init__(self, reference_source: str | None = None) -> None:
        self.reference_source = reference_source
        self._prepared: dict[str, Any] = {"ready": False, "reason": "not prepared"}
        self._sandbox: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    def set_executor(
        self,
        executor: Any,
        compile_fn: Callable[[str, str], Any],
        *,
        comparison_policy: Any = None,
        work_dir: str | None = None,
        limits: Any = None,
    ) -> None:
        """Wire sandbox execution (Phase 4).

        ``executor`` must expose ``run(argv, stdin_text, limits)`` returning
        an object with ``stdout``, ``timed_out``, ``signal`` and ``exit_code``
        (i.e. :class:`ExecutionResult`). ``compile_fn(source_path, out_dir)``
        returns a compile result with ``ok`` / ``binary_path`` /
        ``raw_stderr`` (i.e. :class:`CompileResult`).

        After this, :meth:`prepare` compiles and example-validates the
        reference in the sandbox, and :meth:`assess` runs it per test input
        instead of returning inconclusive.
        """
        import tempfile

        self._sandbox = {
            "executor": executor,
            "compile_fn": compile_fn,
            "policy": comparison_policy,
            "work_dir": work_dir or tempfile.mkdtemp(prefix="ag_reference_"),
            "limits": limits,
            "binary": None,
            "compile_error": None,
        }

    def _sandbox_compile(self, source: str) -> dict[str, Any]:
        """Adapt the sandbox compile_fn to prepare()'s expected shape."""
        assert self._sandbox is not None
        result = self._sandbox["compile_fn"](source, self._sandbox["work_dir"])
        return {
            "ok": bool(result.ok),
            "binary": result.binary_path,
            "diagnostics": result.raw_stderr,
        }

    def _sandbox_run(self, binary: str, stdin_text: str) -> str | None:
        """Adapt the sandbox executor to run_reference's shape."""
        assert self._sandbox is not None
        result = self._sandbox["executor"].run(
            [binary], stdin_text, self._sandbox["limits"]
        )
        if result.timed_out or result.signal is not None or result.exit_code != 0:
            return None
        return result.stdout

    # ------------------------------------------------------------------ #
    def prepare(
        self,
        compile: Callable[[str], dict[str, Any]],
        spec: Specification,
        run_reference: Callable[[str, str], str | None] | None = None,
    ) -> dict[str, Any]:
        """Compile the reference and validate it against spec examples.

        ``compile(source)`` -> {"ok": bool, "binary": path|None, "diagnostics": str}.
        ``run_reference(binary, stdin)`` -> stdout or None; when available the
        spec's worked examples are actually executed — any mismatch downgrades
        the reference (never trusted). When absent, example-validation is
        deferred and recorded honestly.
        """
        from ...core.comparison import compare_output
        from ...core.schemas import ComparisonPolicy

        policy = (
            self._sandbox["policy"]
            if self._sandbox and self._sandbox["policy"] is not None
            else ComparisonPolicy()
        )
        if self._sandbox is not None:
            compile = self._sandbox_compile  # type: ignore[assignment]
            run_reference = self._sandbox_run
        if not self.reference_source:
            self._prepared = {"ready": False, "reason": "no reference solution provided"}
            return self._prepared
        result = compile(self.reference_source)
        if not result.get("ok"):
            self._prepared = {
                "ready": False,
                "reason": "reference failed to compile",
                "diagnostics": result.get("diagnostics", ""),
            }
            return self._prepared
        binary = result.get("binary")
        example_report: Any = (
            "deferred: no executor to run examples" if run_reference is None else None
        )
        if run_reference is not None and spec.examples:
            failures: list[dict[str, Any]] = []
            for ex in spec.examples:
                try:
                    observed = run_reference(binary, ex.input)
                except Exception as exc:
                    observed = None
                    failures.append({"input": ex.input, "reason": f"run failed: {exc}"})
                    continue
                if observed is None or not compare_output(observed, ex.output, policy):
                    failures.append(
                        {
                            "input": ex.input,
                            "spec_output": ex.output,
                            "reference_output": observed,
                            "page": ex.page,
                        }
                    )
            example_report = {
                "examples": len(spec.examples),
                "failed": failures,
            }
            if failures:
                # The reference is not authoritative if it cannot reproduce
                # the assignment's own worked examples — downgrade, loudly.
                self._prepared = {
                    "ready": False,
                    "reason": "reference failed example validation",
                    "example_validation": example_report,
                }
                return self._prepared
        self._prepared = {
            "ready": True,
            "binary": binary,
            "reason": "compiled",
            "example_validation": example_report or "no spec examples to validate against",
        }
        return self._prepared

    def assess(
        self, test_case: TestCase, spec: Specification, ctx: OracleContext
    ) -> OracleVerdict:
        def _inconclusive(reason: str) -> OracleVerdict:
            return OracleVerdict(
                status="inconclusive",
                provenance=ExpectedProvenance(
                    tier="tier1_reference",
                    method="reference_solution",
                    detail=reason,
                ),
            )

        if not self._prepared.get("ready"):
            return _inconclusive(
                f"reference unavailable: {self._prepared.get('reason')}"
            )
        if self._sandbox is not None:
            return self._assess_sandboxed(test_case)
        executor = ctx.reference_executor
        if executor is None:
            # Honest, not mocked: no execution backend wired.
            return _inconclusive("no execution backend wired for the reference")
        try:
            observed = executor(test_case.input)
        except Exception as exc:
            return _inconclusive(f"reference execution failed: {exc}")
        if observed is None:
            return _inconclusive(
                "reference crashed/timed out on this input; "
                "expected output cannot be established"
            )
        return OracleVerdict(
            status="trusted",
            provenance=ExpectedProvenance(
                tier="tier1_reference",
                method="reference_solution",
                detail="expected output produced by the validated reference "
                "implementation",
            ),
            evidence={"reference_stdout": observed},
        )

    # ------------------------------------------------------------------ #
    def _assess_sandboxed(self, test_case: TestCase) -> OracleVerdict:
        """Run the reference in the sandbox for one test input.

        Outcomes: ``trusted`` (output matches the candidate's expected),
        ``rejected`` (deterministic mismatch, with both outputs recorded),
        ``inconclusive`` (reference failed to compile or run — recorded, never
        silently trusted).
        """
        from ...core.comparison import compare_output
        from ...core.schemas import ComparisonPolicy

        def _inconclusive(reason: str, evidence: dict[str, Any] | None = None) -> OracleVerdict:
            return OracleVerdict(
                status="inconclusive",
                provenance=ExpectedProvenance(
                    tier="tier1_reference",
                    method="reference_solution",
                    detail=reason,
                ),
                evidence=evidence or {},
            )

        sb = self._sandbox
        assert sb is not None
        if sb["binary"] is None and sb["compile_error"] is None:
            # Lazy first compile, in the sandbox.
            adapted = self._sandbox_compile(self.reference_source or "")
            if not adapted["ok"]:
                sb["compile_error"] = adapted["diagnostics"]
            else:
                sb["binary"] = adapted["binary"]
        if sb["compile_error"] is not None:
            return _inconclusive(
                f"reference failed to compile in the sandbox: {sb['compile_error'][:500]}"
            )
        try:
            result = sb["executor"].run([sb["binary"]], test_case.input, sb["limits"])
        except Exception as exc:
            return _inconclusive(f"reference execution failed: {exc}")
        if result.timed_out or result.signal is not None or result.exit_code != 0:
            return _inconclusive(
                "reference failed to run on this input "
                f"(timed_out={result.timed_out}, signal={result.signal}, "
                f"exit={result.exit_code}); expected output cannot be established",
                evidence={"stderr_tail": result.stderr[-500:]},
            )
        policy = sb["policy"] or ComparisonPolicy()
        if compare_output(result.stdout, test_case.expected.value, policy):
            return OracleVerdict(
                status="trusted",
                provenance=ExpectedProvenance(
                    tier="tier1_reference",
                    method="reference_solution",
                    detail="expected output produced by the validated reference "
                    "implementation in the sandbox",
                ),
                evidence={"reference_stdout": result.stdout},
            )
        return OracleVerdict(
            status="rejected",
            provenance=ExpectedProvenance(
                tier="tier1_reference",
                method="reference_solution",
                detail="candidate expected output contradicts the sandboxed "
                "reference implementation",
            ),
            evidence={
                "reference_stdout": result.stdout,
                "candidate_expected": test_case.expected.value,
            },
        )


# ---------------------------------------------------------------------------
# Tier 2 — independently verifiable
# ---------------------------------------------------------------------------


class SpecExampleOracle(OracleStrategy):
    """Tier 2 — the assignment's own worked examples.

    If a candidate's (normalized) input matches a spec example's input, the
    expected output MUST equal the spec's stated output: match -> ``trusted``
    (provenance cites the page), mismatch -> ``rejected`` with reason.
    """

    name = "spec_example"
    tier = TIER2

    def assess(
        self, test_case: TestCase, spec: Specification, ctx: OracleContext
    ) -> OracleVerdict:
        key = normalize_ws(test_case.input)
        for ex in spec.examples:
            if normalize_ws(ex.input) == key:
                if normalize_ws(ex.output) == normalize_ws(test_case.expected.value):
                    return OracleVerdict(
                        status="trusted",
                        provenance=ExpectedProvenance(
                            tier="tier2_independent",
                            method="spec_example",
                            detail=(
                                f"expected output matches the assignment's worked "
                                f"example (page {ex.page})"
                            ),
                        ),
                        evidence={"spec_page": ex.page},
                    )
                return OracleVerdict(
                    status="rejected",
                    provenance=ExpectedProvenance(
                        tier="tier2_independent",
                        method="spec_example",
                        detail=(
                            f"candidate expected output contradicts the "
                            f"assignment's worked example (page {ex.page})"
                        ),
                    ),
                    evidence={
                        "spec_page": ex.page,
                        "spec_output": ex.output,
                        "candidate_output": test_case.expected.value,
                    },
                )
        return OracleVerdict(
            status="inconclusive",
            provenance=ExpectedProvenance(
                tier="tier2_independent",
                method="spec_example",
                detail="input matches no worked example in the specification",
            ),
        )


# ---------------------------------------------------------------------------
# Tier 3 — LLM-assisted (agreement is NEVER proof)
# ---------------------------------------------------------------------------

_TIER3_PROMPT_NAME = "oracle_verify"


class LLMProposedOracle(OracleStrategy):
    """Tier 3 — second independent LLM reasoning path + spec checks.

    Outcomes:
    - second path agrees with the candidate  -> ``provisional``
      (NEVER ``trusted`` — enforced below and by test);
    - disagreement                            -> ``inconclusive``;
    - contradiction with a spec example      -> ``rejected``.
    """

    name = "llm_proposed"
    tier = TIER3
    prompt_version = "1.0"

    def assess(
        self, test_case: TestCase, spec: Specification, ctx: OracleContext
    ) -> OracleVerdict:
        def _base(detail: str) -> ExpectedProvenance:
            return ExpectedProvenance(
                tier="tier3_llm", method="llm_independent_path", detail=detail
            )

        # Spec contradiction check first (deterministic).
        for ex in spec.examples:
            if normalize_ws(ex.input) == normalize_ws(test_case.input) and normalize_ws(
                ex.output
            ) != normalize_ws(test_case.expected.value):
                return OracleVerdict(
                    status="rejected",
                    provenance=_base(
                        "candidate contradicts a worked example in the spec; "
                        "no LLM agreement can override that"
                    ),
                )

        if ctx.gateway is None:
            return OracleVerdict(
                status="inconclusive",
                provenance=_base("no LLM gateway available for second path"),
            )
        from ...ollama.models import model_cfg_for
        from ...ollama.prompts import load_prompt

        try:
            model_cfg = dict(model_cfg_for("test_generation"))
            _fm, body = load_prompt(_TIER3_PROMPT_NAME)
            # Independent path: the prompt asset declares its own seed so the
            # second reasoning path does not replay the first one.
            if _fm.get("seed") is not None:
                model_cfg["seed"] = int(_fm["seed"])
            prompt = body.replace(
                "{specification_text}", _spec_text(spec)
            ).replace("{test_input}", test_case.input)
            answer, _record = ctx.gateway.generate_structured(
                task="oracle_second_path",
                prompt=prompt,
                schema=SecondPathAnswer,
                model_cfg=model_cfg,
                prompt_name=_TIER3_PROMPT_NAME,
            )
        except Exception as exc:
            return OracleVerdict(
                status="inconclusive",
                provenance=_base(f"second reasoning path failed: {exc}"),
            )

        if normalize_ws(answer.expected_output) == normalize_ws(test_case.expected.value):
            # INVARIANT: agreement is supporting evidence ONLY. This must stay
            # "provisional" — see test_oracle_tier3_never_trusted.
            status = "provisional"
            assert status != "trusted", "tier-3 agreement must never be trusted"
            return OracleVerdict(
                status=status,
                provenance=_base(
                    "independent second reasoning path agrees with the "
                    "candidate; agreement is supporting evidence only, "
                    "not proof"
                ),
                evidence={"second_path_reasoning": answer.reasoning[:2000]},
            )
        return OracleVerdict(
            status="inconclusive",
            provenance=_base(
                "independent second reasoning path disagrees with the "
                "candidate; expected output remains unverified"
            ),
            evidence={"second_path_output": answer.expected_output[:2000]},
        )


def _spec_text(spec: Specification) -> str:
    parts = []
    for r in spec.requirements:
        parts.append(f"[{r.id} ({r.kind})] {r.text}")
    for ex in spec.examples:
        parts.append(f"[example p{ex.page}] in: {ex.input!r} -> out: {ex.output!r}")
    return "\n".join(parts)[:6000]
