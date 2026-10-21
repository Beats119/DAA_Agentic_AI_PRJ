"""Generation-loop budget/dedup/stop properties (latency Finding 4).

Each property is proven against the production loop with a scripted
TEST-ONLY client: (a) the token budget is checked before every round;
(b) duplicate stdin is dropped across rounds and never re-admitted;
(c) a round with zero new valid candidates stops the loop; (d)
refinement runs only with targets and budget; (e) per-round request
sizes respect the remaining candidate budget.
"""

from __future__ import annotations

import re
from typing import Any

from conftest import make_rules

from autograder.app.core.schemas import (
    GenerationRecord,
    Requirement,
    Specification,
    TestCandidate,
)
from autograder.app.testing.generation.agent import (
    CandidateList,
    generate_test_suite,
)
from autograder.app.testing.generation.budgets import BudgetTracker


class ScriptedClient:
    """TEST-ONLY client: replays queued CandidateLists per task."""

    def __init__(self, script: dict[str, list[list[TestCandidate]]]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def generate_structured(self, *, task: str, prompt: str, schema: type,
                            model_cfg: dict[str, Any],
                            prompt_name: str | None = None,
                            validation_retries: int = 2):
        self.calls.append(task)
        self.prompts.append(prompt)
        queue = self.script.get(task) or [[]]
        candidates = queue.pop(0) if len(queue) > 1 else queue[0]
        record = GenerationRecord(
            task=task, model="fake-test-model", temperature=0.0, seed=42,
            timeout_s=5.0, prompt_version="test-1.0",
            prompt_hash="sha256:" + "c" * 16, duration_s=0.01,
            tokens_used=100,
        )
        return CandidateList(candidates=candidates), record


def _spec() -> Specification:
    return Specification(requirements=[
        Requirement(id="R1", text="Read values and print a result.",
                    kind="explicit"),
        Requirement(id="R2", text="Print the result with a newline.",
                    kind="explicit"),
    ])


def _rules(**gen_over):
    gen = {"max_rounds": 3, "max_candidates": 24, "token_budget": 20000,
           "exec_budget": 30}
    gen.update(gen_over)
    return make_rules([("correctness", "correctness", 1.0)],
                      test_generation=gen)


def _cand(cid: str, inp: str, refs: list[str],
          expected: str = "0") -> TestCandidate:
    return TestCandidate(id=cid, input=inp, input_category="normal",
                         rationale="r", requirement_refs=refs,
                         expected_value=expected)


def test_a_token_budget_checked_before_first_round():
    client = ScriptedClient({"test_generation": [[_cand("T1", "1", ["R1"])]]})
    tracker = BudgetTracker(token_budget=50, exec_budget=30)
    tracker.add_tokens(50)  # already exhausted (e.g. consumed upstream)
    suite = generate_test_suite(_spec(), _rules(), client,
                                budget_tracker=tracker)
    assert client.calls == []
    assert suite["rounds_used"] == 0
    assert any("token budget exhausted before generation started" in lim
               for lim in suite["limitations"])


def test_e_initial_request_respects_remaining_candidate_budget():
    client = ScriptedClient({"test_generation": [[_cand("T1", "1", ["R1", "R2"])]]})
    suite = generate_test_suite(_spec(), _rules(max_candidates=4), client)
    assert suite["rounds_used"] == 1
    m = re.search(r"(\d+) to (\d+) candidates", client.prompts[0])
    assert m is not None
    assert int(m.group(2)) == 4  # asked for at most the remaining budget


def test_b_duplicate_stdin_dropped_across_rounds():
    client = ScriptedClient({
        "test_generation": [[_cand("T1", "1 2", ["R1"], "3"),
                             _cand("T2", "3 4", ["R1"], "7")]],
        # Refinement re-proposes T1's stdin with a DIFFERENT expected
        # value, plus one genuinely new case for R2.
        "test_refinement": [[_cand("T3", "1 2", ["R2"], "999"),
                             _cand("T4", "5 6", ["R2"], "11")]],
    })
    suite = generate_test_suite(_spec(), _rules(), client)
    inputs = [tc.input for tc in suite["candidates"]]
    assert inputs.count("1 2") == 1  # never admitted twice
    assert "5 6" in inputs
    dupes = [q for q in suite["quarantined"]
             if "duplicate" in str(q.get("reason", ""))]
    assert dupes and dupes[0]["id"] == "T3"


def test_c_no_progress_round_stops_the_loop():
    client = ScriptedClient({
        "test_generation": [[_cand("T1", "1", ["R1"])]],
        # Refinement yields only a duplicate id -> zero new valid cases.
        "test_refinement": [[_cand("T1", "2", ["R2"])]],
    })
    suite = generate_test_suite(_spec(), _rules(), client)
    assert suite["rounds_used"] == 2  # stopped; did not burn round 3
    assert client.calls == ["test_generation", "test_refinement"]
    assert any("no new valid candidates" in lim
               for lim in suite["limitations"])


def test_d_no_refinement_without_targets():
    client = ScriptedClient({
        "test_generation": [[_cand("T1", "1", ["R1", "R2"])]],
        "test_refinement": [[_cand("T2", "2", ["R1"])]],
    })
    suite = generate_test_suite(_spec(), _rules(), client)
    assert suite["rounds_used"] == 1
    assert client.calls == ["test_generation"]


def test_d_no_refinement_when_candidate_budget_full():
    client = ScriptedClient({
        "test_generation": [[_cand("T1", "1", ["R1"]),
                             _cand("T2", "2", ["R1"])]],
        "test_refinement": [[_cand("T3", "3", ["R2"])]],
    })
    suite = generate_test_suite(_spec(), _rules(max_candidates=2), client)
    assert len(suite["candidates"]) == 2
    assert client.calls == ["test_generation"]
