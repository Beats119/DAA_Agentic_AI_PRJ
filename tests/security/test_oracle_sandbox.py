"""Tier-1 oracle wired to the real sandbox: compile + run the reference.

Proves the ReferenceSolutionOracle.set_executor path end to end with a real
reference program: trusted on match, rejected on mismatch, inconclusive when
the reference cannot be established. No mocks.
"""

from autograder.app.core.schemas import (
    ComparisonPolicy,
    CompileConfig,
    ExpectedOutput,
    ExpectedProvenance,
    Specification,
    SpecExample,
    TestCase as TestCaseModel,
)
from autograder.app.sandbox import compile_c
from autograder.app.testing.oracle.strategies import OracleContext, ReferenceSolutionOracle

TestCaseModel.__test__ = False  # silence pytest collection of the imported model name

REF_SRC = r"""
#include <stdio.h>
int main() {
    int a, b;
    if (scanf("%d %d", &a, &b) == 2) printf("%d\n", a + b);
    return 0;
}
"""

BROKEN_REF_SRC = "int main( { return 0; }\n"


def _spec():
    return Specification(
        requirements=[],
        examples=[SpecExample(input="2 3\n", output="5\n", page=1)],
    )


def _case(input_text: str, expected: str) -> TestCaseModel:
    return TestCaseModel(
        id="T1",
        input=input_text,
        input_category="normal",
        rationale="oracle wiring",
        requirement_refs=[],
        expected=ExpectedOutput(
            value=expected,
            provenance=ExpectedProvenance(tier="tier3_llm", method="candidate", detail=""),
        ),
        status="provisional",
    )


def _wired_oracle(executor, tmp_path, source: str) -> ReferenceSolutionOracle:
    ref = tmp_path / "ref.c"
    ref.write_text(source)
    oracle = ReferenceSolutionOracle(str(ref))

    def compile_fn(source_path: str, out_dir: str):
        return compile_c(
            source_path, out_dir, CompileConfig(), executor, allowed_dir=tmp_path
        )

    oracle.set_executor(
        executor,
        compile_fn,
        comparison_policy=ComparisonPolicy(),
        work_dir=str(tmp_path / "refwork"),
    )
    return oracle


def test_reference_oracle_trusted_on_match(executor, tmp_path):
    oracle = _wired_oracle(executor, tmp_path, REF_SRC)
    prepared = oracle.prepare(lambda src: {"ok": False}, _spec())
    assert prepared["ready"] is True, f"sandbox prepare failed: {prepared}"

    verdict = oracle.assess(_case("7 8\n", "15\n"), _spec(), OracleContext())
    assert verdict.status == "trusted", f"verdict: {verdict!r}"
    assert verdict.provenance.tier == "tier1_reference"
    assert verdict.evidence["reference_stdout"] == "15\n"


def test_reference_oracle_rejected_on_mismatch(executor, tmp_path):
    oracle = _wired_oracle(executor, tmp_path, REF_SRC)
    oracle.prepare(lambda src: {"ok": False}, _spec())

    verdict = oracle.assess(_case("7 8\n", "16\n"), _spec(), OracleContext())
    assert verdict.status == "rejected", f"verdict: {verdict!r}"
    assert verdict.evidence["reference_stdout"] == "15\n"
    assert verdict.evidence["candidate_expected"] == "16\n"


def test_reference_oracle_inconclusive_when_uncompilable(executor, tmp_path):
    oracle = _wired_oracle(executor, tmp_path, BROKEN_REF_SRC)
    prepared = oracle.prepare(lambda src: {"ok": False}, _spec())
    assert prepared["ready"] is False
    assert "compile" in prepared["reason"]

    verdict = oracle.assess(_case("7 8\n", "15\n"), _spec(), OracleContext())
    assert verdict.status == "inconclusive"
    assert "compile" in verdict.provenance.detail


def test_reference_failing_examples_downgrades(executor, tmp_path):
    # A reference that computes the product, not the sum: it must NOT be
    # trusted even though it compiles and runs.
    oracle = _wired_oracle(
        executor, tmp_path,
        '#include <stdio.h>\nint main(){int a,b;'
        ' if(scanf("%d %d",&a,&b)==2) printf("%d\\n",a*b); return 0;}',
    )
    prepared = oracle.prepare(lambda src: {"ok": False}, _spec())
    assert prepared["ready"] is False
    assert prepared["reason"] == "reference failed example validation"

    verdict = oracle.assess(_case("7 8\n", "56\n"), _spec(), OracleContext())
    assert verdict.status == "inconclusive"
