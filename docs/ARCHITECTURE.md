# Architecture

## Components

| Component | Module | Role |
|-----------|--------|------|
| API + UI | `app/api/` | FastAPI routes, Jinja2 pages (`/`, `/new`, `/rules`, `/operations`, `/runs/{id}`), report rendering |
| Ingestion | `app/ingestion/` | Assignment PDF/text → `AssignmentDoc` (per-page text + sha256) |
| Specification agent | `app/specification/` | LLM extraction → `Specification`: requirements (explicit/inferred/ambiguous + page/excerpt source refs), worked examples, ambiguities, diagnostics |
| Rule registry | `app/rules/` | Load/validate/version/hash instructor rules; invalid configs rejected, never silently normalized |
| Planner | `app/orchestration/planner.py` | Builds the metric-specific evaluation plan from spec + rules |
| Coordinator | `app/orchestration/coordinator.py` | Bounded parallel execution of analyzers (worker pool, per-analyzer timeouts) |
| Ollama gateway | `app/ollama/` | Loopback-only client, structured generation with Pydantic validation, health/model probes, versioned prompt loading |
| Test generation | `app/testing/generation/` | Fresh LLM candidate tests every run; bounded refinement (≤3 rounds), budgets, quarantine with reasons |
| Oracle | `app/testing/oracle/` | Tiered strategies + `OracleService` (tier-preference order, first non-`inconclusive` verdict wins, full provenance chain) |
| Sandbox | `app/sandbox/` | Capability probe, Landlock/netns/rlimit executor, sandboxed compiler |
| Analyzers | `app/analyzers/` | 8 analyzers, common `analyze(ctx) -> AnalyzerResult` contract |
| Evidence registry | `app/orchestration/evidence.py` | Append-only during a run; immutable after finalize; typed refs `ev:<run>:<seq>` |
| Scoring engine | `app/scoring/` | Pure function: `grade = 100 × Σ(w·s)` (Decimal, ROUND_HALF_UP, 1 dp), policy version `1.0` |
| Feedback | `app/orchestration/feedback.py` | LLM draft → deterministic verification → bounded retry → deterministic fallback; never touches grade/evidence |
| Reports | `app/reporting/` + API renderer | JSON + HTML, reconciled against the scoring engine |
| Persistence | `app/persistence/` | SQLite index (runs, rule versions, artifacts) + `artifacts/runs/<run_id>/` |

## Data flow (the flow diagram as code)

`evaluate()` in `app/orchestration/workflow.py`:

1. Fresh `run_id`; sha256 of assignment bytes, source bytes, exact rules config.
2. Rules validated (reject invalid); run state → `validating`.
3. Assignment ingested (pages + hash).
4. **Specification agent** extracts requirements/examples/ambiguities (LLM).
   Explicit vs inferred vs ambiguous kept separate; assumptions never become
   grading rules silently.
5. **Test generation**: fresh LLM candidates every run (categories are
   reasoning prompts, not a fixed suite) → deterministic candidate
   validation → **bounded refinement loop** (max 3 rounds, candidate/token/
   execution budgets) → quarantine with recorded reasons.
6. **Oracle gate**: each candidate assessed by tiered strategies:
   - **Tier 1** — instructor reference solution / expected outputs, compiled
     and run in the same sandbox (the reference itself is validated against
     spec examples; a crashing reference yields `inconclusive`).
   - **Tier 2** — independently verifiable: spec worked examples must match;
     proposed oracle implementations must reproduce them.
   - **Tier 3** — LLM proposes expected outputs; an independent second
     reasoning path + spec checks corroborate. Agreement is supporting
     evidence only → status at best `provisional`, **never `trusted`**
     (enforced by a code assertion + dedicated test).
   
   Every test gets `trusted` / `provisional` / `rejected` / `inconclusive`
   with provenance. **Only `trusted` tests contribute confirmed correctness
   evidence.** Unverifiable candidates are quarantined, never used to fail
   a submission, and the run continues.
7. Student source compiled **in the sandbox** (compilation is untrusted too).
8. Validated `trusted` tests executed in the sandbox with resource limits;
   output/exit/signal/timing/memory collected.
9. Analyzers run (bounded parallel); each returns typed evidence with an
   honest status (`completed`/`failed`/`skipped`/`unsupported`/`inconclusive`).
10. **Evidence registry** finalized (immutable); **scoring engine** computes
    the grade from validated evidence + versioned rules.
11. **Feedback**: LLM draft → deterministic verification (every claimed
    failure must reference a real evidence id; scores must match the
    engine; contradictions rejected) → bounded regeneration → deterministic
    fallback. The feedback model cannot alter grade or evidence.
12. Report validated (`validate_report`: schema + totals reconcile) and
    persisted with full provenance (run/submission/assignment/rule hashes,
    tool + model + prompt versions).

Run states: `queued → validating → planning → generating_tests → executing
→ analyzing → scoring → reporting → completed | completed_with_warnings |
failed | incomplete`. Partial failure degrades honestly; a required metric
without evidence → `incomplete`, never a silent `completed`.

## Ambiguity policy

Unattended execution with explicit uncertainty is the default. Ambiguity-
dependent tests are excluded from confirmed scoring; essential ambiguity
yields `incomplete`/`provisional`, not manufactured certainty. An optional
strict mode can require instructor clarification instead.

## Quarantine bounds

Max 3 candidate-generation rounds per refinement cycle; overall budgets for
candidate count, tokens, and execution — all from the rules file, all
configurable. Never retry an identical invalid candidate indefinitely;
never fail the whole evaluation over unverifiable candidates.
