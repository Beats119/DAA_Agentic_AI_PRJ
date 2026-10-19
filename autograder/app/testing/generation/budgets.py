"""Single home for test-generation budget defaults.

Budgets are *configured* through the rules file
(:class:`TestGenerationPolicy`); this module holds the fallback defaults
used when a policy is absent, and the absolute hard caps that no
configuration may exceed. No magic numbers anywhere else.
"""

from __future__ import annotations

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
