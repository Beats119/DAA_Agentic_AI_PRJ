"""Shared fixtures and builders for unit tests."""

from __future__ import annotations

import pytest

from autograder.app.core.schemas import AnalyzerResult, MetricConfig, RuleSet


def base_rules_dict(**overrides):
    """Minimal valid rules mapping; override any part per-test."""
    data = {
        "problem_id": "p1",
        "assignment_ref": "assignments/p1.pdf",
        "version": "1.0.0",
        "limits": {"time_limit_s": 2.0, "memory_limit_mb": 256.0},
        "metrics": [
            {"name": "correctness", "analyzer": "correctness", "weight": 0.6},
            {"name": "compiler", "analyzer": "compiler", "weight": 0.4},
        ],
    }
    data.update(overrides)
    return data


def make_rules(metrics, **overrides):
    """Build a RuleSet from (name, analyzer, weight, required) tuples."""
    defs = []
    for m in metrics:
        name, analyzer, weight = m[0], m[1], m[2]
        required = m[3] if len(m) > 3 else True
        defs.append(
            {"name": name, "analyzer": analyzer, "weight": weight, "required": required}
        )
    data = base_rules_dict(metrics=defs)
    data.update(overrides)
    return RuleSet.model_validate(data)


def make_result(name, analyzer, score, status="completed"):
    return AnalyzerResult(
        metric_name=name,
        analyzer_id=analyzer,
        analyzer_version="1.0",
        status=status,
        score=score,
    )
