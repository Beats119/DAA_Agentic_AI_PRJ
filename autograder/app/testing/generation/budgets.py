"""Single home for test-generation budget defaults + enforcement.

Budgets are *configured* through the rules file
(:class:`TestGenerationPolicy`); this module holds the fallback defaults
used when a policy is absent, and the absolute hard caps that no
configuration may exceed. No magic numbers anywhere else.

Enforcement (D10): :class:`BudgetTracker` is the per-run counter object.
One tracker is created per evaluation (from the effective budgets) and
shared by the generation loop (token budget), the oracle subsystem and
the correctness analyzer (execution budget). Budgets are checked *before*
the operation that would consume them; exhaustion is surfaced as an
explicit limitation / inconclusive reason, never silently ignored.
"""

from __future__ import annotations

import threading

# Fallback defaults (used only when the rules carry no test-generation policy).
DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_CANDIDATES = 24
DEFAULT_TOKEN_BUDGET = 20000
DEFAULT_EXEC_BUDGET = 120

# Absolute hard caps — configuration may lower these, never raise them.
# (max_rounds is additionally bounded to <= 3 by the TestGenerationPolicy schema.)
HARD_CAP_ROUNDS = 3
HARD_CAP_CANDIDATES = 64
HARD_CAP_TOKEN_BUDGET = 100000
HARD_CAP_EXEC_BUDGET = 600

# Prompt-level guidance (not a budget): how many cases to ask for per round.
MIN_CASES_PER_ROUND = 10
MAX_CASES_PER_ROUND = 16


def effective_budgets(policy) -> dict[str, int]:
    """Merge a :class:`TestGenerationPolicy` (or None) with defaults+caps."""
    max_rounds = getattr(policy, "max_rounds", None) or DEFAULT_MAX_ROUNDS
    max_candidates = getattr(policy, "max_candidates", None) or DEFAULT_MAX_CANDIDATES
    token_budget = getattr(policy, "token_budget", None) or DEFAULT_TOKEN_BUDGET
    exec_budget = getattr(policy, "exec_budget", None) or DEFAULT_EXEC_BUDGET
    return {
        "max_rounds": min(max_rounds, HARD_CAP_ROUNDS),
        "max_candidates": min(max_candidates, HARD_CAP_CANDIDATES),
        "token_budget": min(token_budget, HARD_CAP_TOKEN_BUDGET),
        "exec_budget": min(exec_budget, HARD_CAP_EXEC_BUDGET),
    }


class BudgetTracker:
    """Per-run consumption counter for the token and execution budgets.

    Thread-safe (analyzers run on a thread pool). ``try_consume_exec``
    must be called *before* each sandbox execution charged to the run;
    it returns False — and consumes nothing — once the execution budget
    is exhausted. Token usage is recorded after each LLM call via
    :meth:`add_tokens`; callers check :meth:`tokens_exhausted` before
    starting another generation round.
    """

    def __init__(self, *, token_budget: int, exec_budget: int) -> None:
        if token_budget < 0 or exec_budget < 0:
            raise ValueError("budgets must be >= 0")
        self.token_budget = int(token_budget)
        self.exec_budget = int(exec_budget)
        self._tokens_used = 0
        self._execs_used = 0
        self._lock = threading.Lock()

    @classmethod
    def from_policy(cls, policy) -> "BudgetTracker":
        """Build a tracker from a rules test-generation policy (or None)."""
        eff = effective_budgets(policy)
        return cls(token_budget=eff["token_budget"], exec_budget=eff["exec_budget"])

    # -- tokens ------------------------------------------------------------
    @property
    def tokens_used(self) -> int:
        with self._lock:
            return self._tokens_used

    def add_tokens(self, n: int) -> int:
        """Record ``n`` tokens consumed; returns the new total."""
        if n < 0:
            raise ValueError("token usage cannot be negative")
        with self._lock:
            self._tokens_used += int(n)
            return self._tokens_used

    def tokens_exhausted(self) -> bool:
        with self._lock:
            return self._tokens_used >= self.token_budget

    # -- executions ----------------------------------------------------------
    @property
    def execs_used(self) -> int:
        with self._lock:
            return self._execs_used

    def execs_exhausted(self) -> bool:
        with self._lock:
            return self._execs_used >= self.exec_budget

    def try_consume_exec(self) -> bool:
        """Consume one execution slot *before* running. False = exhausted."""
        with self._lock:
            if self._execs_used >= self.exec_budget:
                return False
            self._execs_used += 1
            return True
