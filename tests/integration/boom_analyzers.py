"""TEST-ONLY analyzer modules for orchestration failure-injection tests.

Analyzers run in worker subprocesses (Phase E), so an in-process
monkeypatch of a production analyzer cannot inject a failure anymore.
Instead, tests point the planner's ``ANALYZER_MODULES`` registry entry at
this module: the worker imports it fresh and its ``analyze`` raises —
the exact production failure path (worker failure envelope -> ``failed``
result). Never imported by production code.
"""

from __future__ import annotations


def analyze(ctx):  # noqa: ANN202 - test double, mirrors analyzer signature
    raise RuntimeError("simulated analyzer crash")
