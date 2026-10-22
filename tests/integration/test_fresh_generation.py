"""Scenario coverage: regeneration + concurrent evaluations.

Two guarantees get explicit tests here:

1. **Forced regeneration is really fresh** — the per-assignment cache
   (see ``tests/unit/test_assignment_cache.py``) reuses the validated
   test set for later evaluations of the same assignment by default,
   but a run that forces regeneration (``force_regenerate=True``, the
   CLI's ``--force-regenerate``) must *call* generation again, and its
   evidence must come from its own fresh response, not the cached
   suite. Default-reuse behavior is pinned by the cache tests.
2. **Concurrent evaluations** — two evaluations running in parallel
   threads complete independently: no shared-state contamination, no
   crossed evidence, no database/artifact interference.

``FakeGateway`` and the fixtures are the TEST-ONLY doubles from
``test_orchestration`` — never used by production code.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from tests.integration.test_orchestration import (  # noqa: F401  (pytest fixtures)
    FakeGateway,
    assignment_pdf,
    rules_path,
    source_path,
)


class CountingGateway(FakeGateway):
    """FakeGateway that tags each test_generation response.

    The n-th ``test_generation`` call returns candidates whose ids carry
    a ``_g<n>`` suffix, so a run's persisted evidence reveals *which*
    generation response it was built from.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gen_calls = 0

    def generate_structured(self, *, task: str, **kw):
        instance, record = super().generate_structured(task=task, **kw)
        if task == "test_generation":
            self.gen_calls += 1
            if self.gen_calls > 1:
                suffix = f"_g{self.gen_calls}"
                instance = instance.model_copy(update={
                    "candidates": [
                        c.model_copy(update={"id": c.id + suffix})
                        for c in instance.candidates
                    ],
                })
        return instance, record


def _run(assignment_pdf, rules_path, source_path, base_dir, **kw):  # noqa: F811 - params shadow imported pytest fixtures
    from autograder.app.orchestration.workflow import evaluate

    return evaluate(
        str(assignment_pdf), str(source_path), str(rules_path),
        base_dir=str(base_dir), max_workers=4, **kw)


def _evidence_text(result) -> str:
    return (Path(result.artifacts_dir) / "evidence.json").read_text()


def test_forced_regeneration_generates_fresh_tests(  # noqa: F811
        assignment_pdf, rules_path, source_path, tmp_path):  # noqa: F811
    """A forced run must call the gateway again and use ITS response.

    Both runs share one artifacts base, so run 2 sits on top of run
    1's cache entry; only the force flag stands between it and reuse.
    """
    gateway = CountingGateway()
    base = tmp_path / "shared"

    r1 = _run(assignment_pdf, rules_path, source_path,
              base, gateway=gateway, run_id="fresh_a")
    assert r1.status == "completed", r1.report.limitations
    calls_after_run1 = gateway.gen_calls
    assert calls_after_run1 >= 1
    assert r1.report.test_stats.generated >= 3
    # Run 1's evidence comes from the first generation response only.
    assert "_g2" not in _evidence_text(r1)

    r2 = _run(assignment_pdf, rules_path, source_path,
              base, gateway=gateway, run_id="fresh_b",
              force_regenerate=True)
    assert r2.status == "completed", r2.report.limitations
    # The gateway was consulted AGAIN — the cached suite was bypassed…
    assert gateway.gen_calls > calls_after_run1
    assert r2.report.test_stats.generated >= 3
    # …and run 2's persisted evidence is built from the later response:
    # it carries the _g2-tagged candidate ids the second call returned.
    assert "_g2" in _evidence_text(r2)


def test_concurrent_evaluations_are_independent(assignment_pdf, rules_path,  # noqa: F811
                                                  source_path, tmp_path):  # noqa: F811
    """Two parallel evaluations complete without contaminating each other."""
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def work(tag: str, run_id: str) -> None:
        try:
            results[tag] = _run(
                assignment_pdf, rules_path, source_path,
                tmp_path / tag, gateway=FakeGateway(), run_id=run_id)
        except BaseException as exc:  # noqa: BLE001 - reported via assert
            errors[tag] = exc

    threads = [
        threading.Thread(target=work, args=("a", "conc_a")),
        threading.Thread(target=work, args=("b", "conc_b")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=600)

    assert not any(t.is_alive() for t in threads), "an evaluation hung"
    assert not errors, errors
    ra, rb = results["a"], results["b"]
    assert ra.status == rb.status == "completed"
    assert ra.report.grade is not None
    # Same inputs + deterministic fake gateway -> same grade, but the
    # evidence of each run is namespaced to its own run id only.
    assert ra.report.grade == rb.report.grade
    for result, own, other in ((ra, "conc_a", "conc_b"),
                               (rb, "conc_b", "conc_a")):
        evidence = json.loads(_evidence_text(result))
        ids = [e["id"] for e in evidence]
        assert ids and all(i.startswith(f"ev:{own}:") for i in ids)
        assert not any(other in i for i in ids)
