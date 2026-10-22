"""Per-assignment cache: specification + oracle-validated test set reuse.

Product policy (owner decision, 2026-10-22): test generation happens
ONCE per assignment. These tests pin the behavior end to end against
the labeled TEST-ONLY ``FakeGateway`` from ``test_orchestration``:

- run 1 generates and writes the cache; run 2 (same assignment +
  rules) makes ZERO specification/generation LLM calls, reuses the
  identical trusted test set, and still executes the student's code;
- the cache key separates assignments (content) and test sets (rules
  hash) while the spec survives a rules change;
- the rules knob and ``force_regenerate`` bypass reads;
- a corrupt cache file is a miss, never a crash;
- a generation with zero trusted tests writes NOTHING, so the next
  run retries instead of inheriting a bad set;
- the fingerprint is a pure, stable function of its inputs.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.integration.test_orchestration import (  # noqa: F401  (fixtures)
    FakeGateway,
    assignment_pdf,
    rules_path,
    source_path,
)

GENERATION_TASKS = ("specification", "test_generation", "test_refinement")


def _run(assignment, rules, source, base_dir, **kw):
    from autograder.app.orchestration.workflow import evaluate

    return evaluate(
        str(assignment), str(source), str(rules),
        base_dir=str(base_dir), max_workers=4, **kw)


def _task_calls(gateway: FakeGateway, task: str) -> int:
    return gateway.calls.count(task)


def _manifest(result) -> dict:
    return json.loads(
        (Path(result.artifacts_dir) / "manifest.json").read_text())


def _executed_test_ids(result) -> list[str]:
    evidence = json.loads(
        (Path(result.artifacts_dir) / "evidence.json").read_text())
    return sorted(
        str(e["data"].get("id") or e["data"].get("test_id"))
        for e in evidence
        if e.get("kind") == "test_result" and e.get("data")
    )


def _cache_files(base_dir: Path) -> list[Path]:
    cache_dir = base_dir / "assignment_cache"
    if not cache_dir.exists():
        return []
    return sorted(cache_dir.glob("*.json"))


# --------------------------------------------------------------------------- #
# Roundtrip: generate once, reuse afterwards
# --------------------------------------------------------------------------- #

def test_second_run_reuses_cached_spec_and_testset(
        assignment_pdf, rules_path, source_path, tmp_path):  # noqa: F811
    base = tmp_path / "artifacts"

    gw1 = FakeGateway()
    r1 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=gw1, run_id="cache_r1")
    assert r1.status == "completed", r1.report.limitations
    assert r1.report.test_stats.trusted >= 1
    assert _task_calls(gw1, "specification") == 1
    assert _task_calls(gw1, "test_generation") >= 1

    files = _cache_files(base)
    assert len(files) == 1, "run 1 must write exactly one cache entry"

    gw2 = FakeGateway()
    r2 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=gw2, run_id="cache_r2")
    assert r2.status == "completed", r2.report.limitations

    # ZERO LLM work for spec extraction and test generation on reuse.
    for task in GENERATION_TASKS:
        assert _task_calls(gw2, task) == 0, f"run 2 called {task}"

    # The identical trusted set was used, and the student's code was
    # still executed against it in the sandbox on run 2.
    assert r2.report.test_stats.trusted == r1.report.test_stats.trusted
    assert r2.report.test_stats.generated == r1.report.test_stats.generated
    assert _executed_test_ids(r2) == _executed_test_ids(r1)
    assert _executed_test_ids(r2), "run 2 executed no tests"
    assert r2.report.test_stats.executed == r2.report.test_stats.trusted
    diag2 = _manifest(r2)["diagnostics"]
    assert diag2["sandbox_executions"] > 0

    # Provenance: diagnostics + the plainly worded report note.
    assert diag2["cache_spec_hit"] is True
    assert diag2["cache_testset_hit"] is True
    assert diag2["cache_source_run_id"] == "cache_r1"
    assert diag2["cache_source_created_at"]
    assert len(diag2["cache_fingerprint"]) == 16
    notes = r2.report.reproduction.notes
    assert "Test set reused from run cache_r1 (generated " in notes
    assert "no new tests were generated for this run" in notes
    # Run 1's own report must NOT claim reuse.
    assert "Test set reused" not in r1.report.reproduction.notes
    diag1 = _manifest(r1)["diagnostics"]
    assert diag1["cache_spec_hit"] is False
    assert diag1["cache_testset_hit"] is False

    # Same evidence -> same grade.
    assert r2.report.grade == r1.report.grade


def test_cache_hit_works_without_gateway(
        assignment_pdf, rules_path, source_path, tmp_path):  # noqa: F811
    """A cached assignment still grades when no LLM is available.

    Correctness evidence comes entirely from the cache + fresh sandbox
    execution, so a grade is issued. The run lands at
    ``completed_with_warnings`` (not ``completed``) because the
    readability analyzer's LLM review genuinely cannot run without a
    gateway — its metric is honestly missing and weights renormalize,
    so the grade need not equal the gateway run's.
    """
    base = tmp_path / "artifacts"
    r1 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=FakeGateway(), run_id="cache_ng1")
    assert r1.status == "completed", r1.report.limitations

    r2 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=None, run_id="cache_ng2")
    assert r2.status == "completed_with_warnings", r2.report.limitations
    assert r2.report.grade is not None
    assert r2.report.test_stats.trusted == r1.report.test_stats.trusted
    assert _executed_test_ids(r2) == _executed_test_ids(r1)
    diag2 = _manifest(r2)["diagnostics"]
    assert diag2["cache_spec_hit"] is True
    assert diag2["cache_testset_hit"] is True


# --------------------------------------------------------------------------- #
# Key separation: assignment content vs rules hash
# --------------------------------------------------------------------------- #

def test_different_assignment_content_misses(
        assignment_pdf, rules_path, source_path, tmp_path):  # noqa: F811
    base = tmp_path / "artifacts"
    r1 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=FakeGateway(), run_id="cache_da1")
    assert r1.status == "completed", r1.report.limitations
    assert len(_cache_files(base)) == 1

    other = tmp_path / "assignment2.txt"
    other.write_text(
        "Assignment: Multiply Two Integers\n\nWrite a C program that "
        "reads two integers and prints their product.\n")
    gw2 = FakeGateway()
    r2 = _run(other, rules_path, source_path, base,
              gateway=gw2, run_id="cache_da2")
    assert r2.status == "completed", r2.report.limitations
    assert _task_calls(gw2, "specification") == 1
    assert _task_calls(gw2, "test_generation") >= 1
    assert len(_cache_files(base)) == 2


def test_different_rules_spec_hits_testset_misses(
        assignment_pdf, rules_path, source_path, tmp_path):  # noqa: F811
    base = tmp_path / "artifacts"
    r1 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=FakeGateway(), run_id="cache_dr1")
    assert r1.status == "completed", r1.report.limitations

    data = json.loads(Path(rules_path).read_text())
    data["similarity"]["threshold"] = 0.85  # same policy shape, new hash
    rules2 = tmp_path / "rules2.json"
    rules2.write_text(json.dumps(data))

    gw2 = FakeGateway()
    r2 = _run(assignment_pdf, rules2, source_path, base,
              gateway=gw2, run_id="cache_dr2")
    assert r2.status == "completed", r2.report.limitations
    # Spec is assignment-only: reused. Test set is rules-keyed: fresh.
    assert _task_calls(gw2, "specification") == 0
    assert _task_calls(gw2, "test_generation") >= 1
    diag2 = _manifest(r2)["diagnostics"]
    assert diag2["cache_spec_hit"] is True
    assert diag2["cache_testset_hit"] is False
    # The entry now carries BOTH rules' test sets.
    entry = json.loads(_cache_files(base)[0].read_text())
    assert len(entry["testsets"]) == 2


# --------------------------------------------------------------------------- #
# Kill switches
# --------------------------------------------------------------------------- #

def test_reuse_knob_off_regenerates(
        assignment_pdf, rules_path, source_path, tmp_path):  # noqa: F811
    base = tmp_path / "artifacts"
    r1 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=FakeGateway(), run_id="cache_knob1")
    assert r1.status == "completed", r1.report.limitations
    assert len(_cache_files(base)) == 1

    data = json.loads(Path(rules_path).read_text())
    data["test_generation"]["reuse_cached_testset"] = False
    rules_off = tmp_path / "rules_off.json"
    rules_off.write_text(json.dumps(data))

    gw2 = FakeGateway()
    r2 = _run(assignment_pdf, rules_off, source_path, base,
              gateway=gw2, run_id="cache_knob2")
    assert r2.status == "completed", r2.report.limitations
    # Even the spec (whose key ignores rules) is re-extracted: reads off.
    assert _task_calls(gw2, "specification") == 1
    assert _task_calls(gw2, "test_generation") >= 1
    diag2 = _manifest(r2)["diagnostics"]
    assert diag2["cache_spec_hit"] is False
    assert diag2["cache_testset_hit"] is False


def test_force_regenerate_bypasses_reads_but_updates_cache(
        assignment_pdf, rules_path, source_path, tmp_path):  # noqa: F811
    base = tmp_path / "artifacts"
    r1 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=FakeGateway(), run_id="cache_fr1")
    assert r1.status == "completed", r1.report.limitations

    gw2 = FakeGateway()
    r2 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=gw2, run_id="cache_fr2", force_regenerate=True)
    assert r2.status == "completed", r2.report.limitations
    assert _task_calls(gw2, "specification") == 1
    assert _task_calls(gw2, "test_generation") >= 1
    assert _manifest(r2)["diagnostics"]["cache_testset_hit"] is False

    # The forced run refreshed the cache: a normal run hits again, and
    # now names the FORCED run as its source.
    gw3 = FakeGateway()
    r3 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=gw3, run_id="cache_fr3")
    assert r3.status == "completed", r3.report.limitations
    for task in GENERATION_TASKS:
        assert _task_calls(gw3, task) == 0
    assert _manifest(r3)["diagnostics"]["cache_source_run_id"] == "cache_fr2"


# --------------------------------------------------------------------------- #
# Failure hygiene
# --------------------------------------------------------------------------- #

def test_corrupt_cache_file_is_a_miss_not_a_crash(
        assignment_pdf, rules_path, source_path, tmp_path):  # noqa: F811
    base = tmp_path / "artifacts"
    r1 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=FakeGateway(), run_id="cache_cc1")
    assert r1.status == "completed", r1.report.limitations
    files = _cache_files(base)
    assert len(files) == 1
    files[0].write_bytes(b"\x00\x01 not json at all {{{")

    gw2 = FakeGateway()
    r2 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=gw2, run_id="cache_cc2")
    assert r2.status == "completed", r2.report.limitations
    assert _task_calls(gw2, "specification") == 1
    assert _task_calls(gw2, "test_generation") >= 1
    # The successful fresh run repaired the entry.
    entry = json.loads(files[0].read_text())
    assert entry["spec"]["source_run_id"] == "cache_cc2"


class UntrustedGateway(FakeGateway):
    """Candidates no oracle tier can trust (spec-example conflict +
    an unverifiable guess; Tier-3 is off by default)."""

    def _response_for(self, task: str, prompt: str):
        if task == "test_generation":
            from autograder.app.core.schemas import TestCandidate
            from autograder.app.testing.generation.agent import CandidateList

            return CandidateList(candidates=[
                TestCandidate(
                    id="u1", input="2 3", input_category="normal",
                    rationale="contradicts the spec's worked example",
                    requirement_refs=["req1", "req2"], expected_value="6"),
                TestCandidate(
                    id="u2", input="7 7", input_category="normal",
                    rationale="no tier can verify this",
                    requirement_refs=["req1", "req2"], expected_value="13"),
            ])
        return super()._response_for(task, prompt)


def test_zero_trusted_generation_writes_no_cache(
        assignment_pdf, rules_path, source_path, tmp_path):  # noqa: F811
    base = tmp_path / "artifacts"
    r1 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=UntrustedGateway(), run_id="cache_zt1")
    assert r1.report.test_stats.trusted == 0
    assert _cache_files(base) == [], (
        "a generation with zero trusted tests must not be cached")

    # The next run must retry generation, not inherit the bad set.
    gw2 = UntrustedGateway()
    r2 = _run(assignment_pdf, rules_path, source_path, base,
              gateway=gw2, run_id="cache_zt2")
    assert _task_calls(gw2, "test_generation") >= 1
    assert r2.report.test_stats.trusted == 0
    assert _cache_files(base) == []


# --------------------------------------------------------------------------- #
# Fingerprint + storage units
# --------------------------------------------------------------------------- #

def test_fingerprint_is_stable_and_input_sensitive():
    from autograder.app.orchestration.cache import build_fingerprint

    a = build_fingerprint(b"the assignment bytes")
    b = build_fingerprint(b"the assignment bytes")
    assert a is not None and b is not None
    # Pure function: identical inputs -> identical fingerprint, across
    # calls (and therefore across processes); no timestamps inside.
    assert a.fingerprint == b.fingerprint
    assert a.model_names == b.model_names
    assert a.prompt_versions == b.prompt_versions
    assert set(a.prompt_versions) == {
        "specification", "test_generation", "test_refinement"}

    c = build_fingerprint(b"different assignment bytes")
    assert c is not None and c.fingerprint != a.fingerprint


def test_cache_entry_roundtrip_and_corruption(tmp_path):
    from autograder.app.core.schemas import (
        ExpectedOutput,
        ExpectedProvenance,
        Specification,
        TestCase,
    )
    from autograder.app.orchestration.cache import (
        AssignmentCacheEntry,
        CachedSpec,
        CachedTestSet,
        cache_path,
        load_cache_entry,
        store_cache_entry,
    )
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    spec = Specification(requirements=[], examples=[], ambiguities=[])
    case = TestCase(
        id="t1", input="2 3", input_category="normal",
        expected=ExpectedOutput(
            value="5",
            provenance=ExpectedProvenance(
                tier="tier2_independent", method="spec_example")),
        status="trusted")
    entry = AssignmentCacheEntry(
        fingerprint="ab" * 32,
        created_at=now,
        model_names={"specification": "m", "test_generation": "m"},
        prompt_versions={"specification": "1.1"},
        prompt_hashes={"specification": "sha256:" + "0" * 16},
        spec=CachedSpec(created_at=now, source_run_id="run_a",
                        specification=spec),
        testsets={"sha256:rules": CachedTestSet(
            created_at=now, source_run_id="run_a",
            rules_hash="sha256:rules", tests=[case],
            counts={"generated": 1, "validated": 1, "rejected": 0,
                    "trusted": 1})},
    )
    store_cache_entry(tmp_path, entry)
    loaded = load_cache_entry(tmp_path, "ab" * 32)
    assert loaded is not None
    assert loaded.spec.specification == spec
    assert loaded.testsets["sha256:rules"].tests[0].status == "trusted"
    assert loaded.testsets["sha256:rules"].tests[0].expected.value == "5"

    # Misses: unknown fingerprint, garbage bytes, wrong format version.
    assert load_cache_entry(tmp_path, "cd" * 32) is None
    cache_path(tmp_path, "ab" * 32).write_bytes(b"garbage{{{")
    assert load_cache_entry(tmp_path, "ab" * 32) is None
    bad = entry.model_copy(update={"format_version": 999})
    store_cache_entry(tmp_path, bad)
    assert load_cache_entry(tmp_path, "ab" * 32) is None
