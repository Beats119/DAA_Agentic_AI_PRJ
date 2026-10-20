# Repeated-Run Variance

**Acceptance bar (docs/PLAN.md §6, item 17): max grade deviation ≤ 7.0 across
repeated runs of the same submission + assignment + rules.**

## Measurement (2026-10-20, this VM)

Harness: `/tmp/variance_run.py` (test-only, not shipped) — runs the real
`autograder.app.orchestration.workflow.evaluate()` 5 times on
`tests/fixtures/c/correct_add.c` with a synthetic assignment PDF and
`examples/rules/add.json`, using the **TEST-ONLY FakeGateway** from
`tests/integration/test_orchestration.py` (deterministic canned LLM
responses, clearly labeled, never in production code).

| Run | Status | Grade |
|-----|--------|-------|
| variance_00 | completed | 95.3 |
| variance_01 | completed | 95.3 |
| variance_02 | completed | 95.3 |
| variance_03 | completed | 95.3 |
| variance_04 | completed | 95.3 |

**Max deviation: 0.00 → PASS** (bar: ≤ 7.0).

Degraded-mode pair (`--no-llm`, no gateway at all):

| Run | Status | Grade |
|-----|--------|-------|
| variance_nollm_00 | incomplete | None |
| variance_nollm_01 | incomplete | None |

Both identical — the degraded path is deterministic too.

## Why variance can occur with a real LLM

Two layers, kept strictly separate:

1. **Scoring is deterministic given evidence.** The scoring engine
   (`autograder/app/scoring/engine.py`) is a pure function:
   `grade = 100 × Σ(w_i × s_i)` in Decimal with ROUND_HALF_UP to 1 decimal.
   Identical validated evidence + identical rules → identical score, always.
   The 0.00 above is this property, measured end to end.

2. **Evidence can vary slightly because test generation is fresh every run —
   by design.** Each run asks the LLM for new candidate tests and new spec
   interpretations. A different candidate set (or a test whose oracle verdict
   lands `provisional` instead of `trusted` on a borderline case) can move
   the correctness score by a few points. This is the documented cost of the
   no-memoization requirement, and the ±7.0 bar is the accepted envelope.

## What keeps real-LLM variance inside the bar

- Temperature 0 + fixed seed per task (`configs/models.yaml`).
- Oracle trust gate: only `trusted` tests feed confirmed correctness; a
  flaky LLM guess can at worst become `provisional`/`inconclusive`, never a
  fabricated failure.
- Versioned prompts + recorded `GenerationRecord`s (model id, settings,
  prompt hash) in every report, so any grade change between runs is
  explainable from the provenance trail.

## Not measured here

True LLM-driven variance (5 runs against real Ollama + llama3.1) cannot be
measured on this VM — no Ollama is installed here. Run
`/tmp/variance_run.py`'s real equivalent on the target machine (Ollama
reachable) and compare against the 7.0 bar before claiming it.
