# AutoGrader — Implementation Plan (reconciled)

Source documents: the architecture diagram, the flow diagram, the architectural
clarifications ("latest clarifications"), and the Master Development Prompt.
Where they overlap, the latest clarifications take precedence.

## 1. Reconciliation

**Consistent (clarifications more specific → they govern):**
- Tiered oracle (Tier 1 reference / Tier 2 independently verifiable / Tier 3
  LLM-assisted) with per-test oracle status
  `trusted | provisional | rejected | inconclusive` + provenance.
- Quarantine: max 3 generation rounds per refinement cycle; overall budgets
  (candidate count, tokens, execution); everything configurable; never fail the
  whole run over unverifiable candidates.
- Ambiguity: unattended by default, explicit uncertainty; explicit vs inferred
  vs ambiguous requirements kept separate; ambiguity-dependent tests excluded
  from confirmed scoring; optional strict mode.
- Feedback verification: deterministic checks first (evidence IDs, schemas,
  no contradictions), optional supplementary LLM pass, bounded retry,
  deterministic fallback; feedback never alters grade/evidence.
- FastAPI + Jinja2 + vanilla JS. No Streamlit as primary UI.
- Ollama-only, no API keys, loopback only.

**Tensions resolved:**
- *T1 — "fresh generation every run" vs "persist validated evidence where
  policy permits" (§15).* Fresh LLM test generation is **mandatory every run**
  (no cross-run test/grade caching — that would fake a fresh run). Persistence
  is for provenance/audit and optional in-run regression evidence only.
- *T2 — "same grade when validated evidence is the same" (§2) vs the ±5–7
  stability bar.* No contradiction: **scoring is deterministic given evidence**;
  evidence may vary slightly across runs because generation is fresh.
  Acceptance threshold for repeated-run variance: **max deviation ≤ 7.0**
  grade points. Zero variance is never claimed (§15).
- *T3 — instructor tests (Tier 1) vs "don't replace generation with manual
  cases" (§10).* Instructor-provided tests/expected outputs are Tier-1 oracle
  evidence (labeled, provenanced). The LLM still generates fresh candidates
  every run; manual tests never replace the generation step.
- *T4 — "preserve existing work" (Phase 1) vs the reset.* The new repo starts
  clean. The previous implementation (kept locally as reference) informs
  design; everything is reimplemented into the new architecture.

**Remaining contradictions:** none blocking. Judgment calls are documented
inline as `DECISION:` comments.

## 2. Architecture (from the diagrams, reconciled)

```
Web App (Jinja2 + vanilla JS)
  Instructor workspace | Assignment PDF upload | Submission upload
  Evaluation dashboard | Reports & run history
        |
Backend & Agent Orchestrator (FastAPI)
  API layer & validation | Document ingestion | Specification agent
  External rule registry (versioned) | Evaluation planner | Agent coordinator
  Evidence registry (typed, immutable after finalize)
        |
  +-- Deterministic: scoring engine | report validation | JSON+HTML reports
  +-- Ollama runtime (gateway): test generation agent | test refinement agent
  |                             code-quality review agent | feedback agent
  +-- Deterministic analysis + secure execution: sandbox (isolated Linux),
      correctness | compiler | static analysis | modularity |
      complexity & performance | memory safety | similarity | oracle validation
        |
Persistent storage: SQLite (run metadata, index) + artifact dir (immutable files)
```

**Oracle decision procedure (design; validates against clarifications):**
1. Extract spec examples (input→output pairs stated in the PDF) — these are
   Tier-2 anchors with page provenance. Any proposed oracle must reproduce them.
2. Tier 1: instructor reference solution / expected outputs (per rules config).
   Reference is compiled and run in the sandbox; it is NOT trusted blindly —
   it must reproduce spec examples and be self-consistent, else downgraded.
3. Tier 2: LLM proposes an *oracle implementation* (small program computing
   expected outputs); validated against spec examples + spec invariants.
4. Tier 3: LLM proposes expected outputs directly; independent second
   reasoning path + spec checks. Agreement = supporting evidence only →
   status at best `provisional`, never `trusted` on agreement alone.
5. Only `trusted` tests contribute confirmed pass/fail correctness evidence.
   `provisional`/`inconclusive` tests run for exploration, reported
   separately, excluded from confirmed scoring. `rejected` → quarantined with
   reason recorded.

## 3. Component contracts

- **Analyzer contract**: typed input (source path, binary path, config subset,
  evidence refs) → typed result (metric name, analyzer id+version, status
  `completed|failed|skipped|unsupported|inconclusive`, raw measurements,
  normalized score 0–1 or null, evidence refs, findings, warnings, diagnostics,
  execution metadata). Analyzers never see the full run state.
- **Agent contract (Ollama gateway)**: task name → versioned prompt template +
  Pydantic response schema + timeouts/retries/budgets → validated structured
  output. Gateway records model id, settings, prompt version. No hosted calls.
- **Evidence registry**: append-only during a run; typed refs
  (`ev:<run>:<seq>`); immutable after finalize; large artifacts stored by
  content hash, referenced by ID.
- **Scoring engine**: pure function
  `(metric results, versioned rules) → grade`, fixed decimal precision,
  versioned normalization policies, explicit missing-evidence policy.
  No LLM dependency.

## 4. Configuration schemas

- `rules/*.json|yaml` (versioned, sha256-hashed): problem/assignment id, spec
  version ref, limits (time/mem/process/output), compilation config, metrics
  [{name, analyzer, weight, params, thresholds}], correctness comparison
  policy, test-generation budgets {rounds≤3, max candidates, token budget,
  exec budget}, oracle policy {tier preference, reference path}, complexity
  requirements, similarity thresholds, uncertainty & missing-evidence policy
  {required-metric-missing → incomplete | optional → policy}, report config,
  model/task settings. Weights must sum to 1 within 1e-9 (reject otherwise;
  no silent normalization unless explicitly enabled).
- `configs/models.yaml`: task → model name + parameters (temperature, seed,
  timeout, max tokens). Defaults to `llama3.1`; startup probe lists actually
  available models and warns.
- `.env` (from `.env.example`): Ollama URL (default loopback), artifact dir,
  DB path, sandbox policy.

## 5. Implementation phases

- **P1 — Inspect & plan** (this doc). DONE.
- **P2 — Core foundations**: pyproject/deps, Pydantic schemas, rules
  validation+versioning+hashing, SQLite persistence, scoring engine, report
  schemas, unit tests. Gate: unit tests green.
- **P3 — Local AI integration**: Ollama gateway (+health/model probe), prompt
  templates (versioned), spec extraction agent, test generation + bounded
  refinement, oracle validation subsystem. Gate: mock-Ollama integration
  tests; real-Ollama tests skipped-with-reason here, run on target machine.
- **P4 — Secure execution**: sandbox (namespaces/seccomp/cgroups where
  available, always: resource limits, process-group kill, output caps,
  no-network, temp per-run dir, fail-closed), adversarial tests. Gate:
  hang/crash/fork-bomb/memory-hog contained; missing isolation → fail closed.
- **P5 — Analyzers**: correctness, compiler, static analysis, modularity,
  complexity, memory safety, readability (LLM), similarity. Typed evidence,
  explicit statuses. Gate: each analyzer independently validated.
- **P6 — Orchestration**: full workflow, run states
  (queued→…→completed/completed_with_warnings/failed/incomplete),
  bounded concurrency, partial-failure handling, evidence-gated scoring.
  Gate: end-to-end on fixtures without LLM (skips explicit).
- **P7 — Reports & frontend**: JSON+HTML reports (totals reconcile), FastAPI
  routes (health, assignments, rules, submissions, evaluations, reports,
  history, artifacts), Jinja2 UI (instructor, submission, dashboard, reports,
  operations). Gate: API exercised end-to-end via tests.
- **P8 — Verification**: full test suite, documented real demo, repeated-run
  variance measurement (≤7.0), fix failures, clean-env install check.
- **P9 — Final handoff**: summary, file list, setup/launch/test commands,
  test report, limitations, demo results.

## 6. Acceptance checklist (from §27 + reconciled bars)

1. App starts locally, usable web UI. 2. No external inference/API keys.
3. Ollama+model availability checked at startup/operations page.
4. Rules editable without code changes. 5. Fresh LLM test generation per run.
6. Tests validated before execution. 7. Untrusted C in verified isolation.
8. Timeouts/resource exhaustion handled. 9. Structured schemas + evidence refs.
10. Deterministic scoring, never LLM-chosen. 11. Same evidence+rules →
    same score. 12. Unverified expected outputs never become confirmed
    failures. 13. Missing required analyzers can't silently score full.
14. JSON/HTML reconcile with score. 15. Findings traceable to evidence.
16. Similarity flags ≠ cheating proof. 17. Repeated-run variance measured,
    max deviation ≤ 7.0. 18. Tests run with honest pass/fail/skip.
19. Real end-to-end demo documented. 20. Security boundary documented.
21. Setup instructions + example rules + final-report material.
22. Nothing mocked presented as real.

## 7. Environment notes (this VM)

Python 3.12.3 ✓, gcc 13.3 ✓. Missing: Ollama, cppcheck, valgrind, lizard,
perf → dependent analyzers/tests degrade to `unsupported`/`skipped` with
explicit diagnostics (never silent). 2 CPU / 7 GB RAM here; target is 16 GB
(Khushi's machine). Sandbox capability probe is part of P4.
