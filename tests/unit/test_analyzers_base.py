"""Contract tests for the analyzer base: status/score honesty + evidence."""

import pytest

from autograder.app.analyzers.base import AnalyzerContext, build_result
from autograder.app.core.schemas import Evidence, MetricConfig


def _metric():
    return MetricConfig(name="m", analyzer="m", weight=1.0)


def test_failed_must_not_carry_score():
    with pytest.raises(ValueError, match="must not carry a score"):
        build_result(metric=_metric(), analyzer_id="x", status="failed", score=0.5)


def test_inconclusive_must_not_carry_score():
    with pytest.raises(ValueError, match="must not carry a score"):
        build_result(metric=_metric(), analyzer_id="x", status="inconclusive", score=0.0)


def test_unsupported_must_not_carry_score():
    with pytest.raises(ValueError, match="must not carry a score"):
        build_result(metric=_metric(), analyzer_id="x", status="unsupported", score=1.0)


def test_score_out_of_range_rejected():
    with pytest.raises(ValueError, match="outside"):
        build_result(metric=_metric(), analyzer_id="x", status="completed", score=1.5)


def test_completed_result_shape():
    r = build_result(
        metric=_metric(), analyzer_id="x", status="completed", score=0.75,
        evidence_refs=["ev:test:1"],
    )
    assert r.score == 0.75
    assert r.analyzer_version == "x-1.0"
    assert r.evidence_refs == ["ev:test:1"]


def test_evidence_minting(tmp_path):
    ctx = AnalyzerContext(
        source_path="s.c", binary_path=None, sanitizer_binary_path=None,
        rules=None, metric=_metric(), run_dir=str(tmp_path),
        executor=None, compiler=None, run_id="r1",
    )
    id1 = ctx.add_evidence("x", "kind_a", {"k": 1})
    id2 = ctx.add_evidence("x", "kind_b", {"k": 2}, severity="high")
    assert id1 == "ev:r1:1"
    assert id2 == "ev:r1:2"
    assert len(ctx.evidence) == 2
    assert all(isinstance(e, Evidence) for e in ctx.evidence)
    assert ctx.evidence[1].severity == "high"
    # Evidence is immutable.
    with pytest.raises(Exception):
        ctx.evidence[0].data = {}
