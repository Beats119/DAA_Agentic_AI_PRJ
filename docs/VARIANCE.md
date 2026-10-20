# Repeated-Run Variance

**Acceptance bar (docs/PLAN.md §6, item 17): max grade deviation ≤ 7.0 across
repeated runs of the same submission + assignment + rules.**

> **Status (2026-10-20):**
> - **Deterministic-path variance: VERIFIED — 0.00.** The figure below
>   was measured with the **deterministic FakeGateway** (a test double),
>   so it proves the scoring layer's determinism end-to-end, *not* the
>   real model's stability.
> - **Real-model variance: UNVERIFIED — BLOCKED–ENVIRONMENT.** No
>   Ollama is installed on this VM, so no run against a real LLM has
>   ever been measured against the ≤ 7.0 bar. Do not cite 0.00 as a
>   real-model result. Unblock: run the 5-run real-gateway procedure
>   in [docs/OLLAMA.md](docs/OLLAMA.md) on the target machine and
>   record the outcome here.

## Measurement (2026-10-20, this VM)

Harness: a throwaway script (test-only, not shipped) ran the real
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
measured on this VM — no Ollama is installed here. Run the equivalent
procedure against the real gateway on the target machine (exact steps:
[docs/OLLAMA.md](docs/OLLAMA.md), "Real-model verification") and compare
against the 7.0 bar before claiming it. Until that run is recorded here,
the real-model status stays **UNVERIFIED**.
