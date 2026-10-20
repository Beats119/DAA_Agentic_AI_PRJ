# Implementation Timeline

Rebuild of AutoGrader in `Beats119/DAA_Agentic_AI_PRJ` (the repo previously
held only `Agentic-AI-assignment.pdf`), per the reconciled master spec
(`docs/PLAN.md`). Every phase was verified by direct execution before the
next began.

## Phase 1 — Inspect & plan (2026-10-20)

Inspected the new repo (clean, PDF only) and the build VM (Python 3.12,
gcc 13.3, **no** Ollama/cppcheck/valgrind/lizard/perf → explicit
skip/unsupported paths). Reconciled the master prompt with the architectural
clarifications (tiered oracle + statuses, quarantine bounds, ambiguity
policy, hybrid feedback verification, FastAPI+Jinja2, Ollama-only). Key
resolutions: fresh LLM test generation mandatory every run (persistence is
provenance-only); scoring deterministic given evidence; repeated-run bar
max deviation ≤ 7.0. Wrote `docs/PLAN.md`.

## Phase 2 — Core foundations

`pyproject.toml`, Pydantic v2 schemas (RuleSet, Specification, TestCase,
Evidence, AnalyzerResult, Report, RunManifest), rules loader/validator
(rejects bad weights, bad analyzer ids, negative limits — no silent
normalization), versioning + sha256 hashing, SQLite persistence (12-state
run machine, atomic artifacts, immutable after terminal states),
deterministic scoring engine (Decimal, ROUND_HALF_UP, 1 dp, policy 1.0,
missing-evidence policies), report schema + reconciliation validation,
6 versioned prompt drafts, example rules (8 metrics, weights sum 1.0).
**90/90 unit tests.** Bug fixed: "exclude" policy incorrectly renormalizing.

## Phase 3 — Local AI integration

Ollama gateway (loopback-only construction, no auth headers ever, bounded
retries, `generate_structured` with Pydantic validation + GenerationRecord),
health/model probes with actionable `ollama pull` guidance, PDF ingestion
(PyMuPDF, per-page text + hash), spec extraction agent
(explicit/inferred/ambiguous + source refs), test generation + bounded
refinement (≤3 rounds, budgets, quarantine with reasons), tiered oracle
subsystem (Tier-1 reference interface, Tier-2 spec-example anchor, Tier-3
LLM proposal with independent second path; **agreement capped at
`provisional` by code assertion**). 6 prompts fleshed out to v1.0.
**117 passed + 4 skipped** (real-Ollama tests skip explicitly).

## Phase 4 — Secure execution

Capability probe (honest: netns yes, userns no, cgroup v2 no, rlimits yes,
Landlock ABI v1), executor with Landlock fs containment (per-run fail-closed,
child exits 125 before exec), netns where available, rlimits + killpg tree
kill + output caps + sanitized env, sandboxed compilation, Tier-1 oracle
wired to genuinely example-validate the reference (crash/timeout →
`inconclusive`). **19/19 adversarial security tests green**; full suite
136 passed + 4 skipped. `docs/SANDBOX.md` written.

## Phase 5 — Analyzer farm

8 analyzers on the common typed contract: correctness (only `trusted`
tests score; zero trusted → `inconclusive`), compiler (warning penalties),
static_analysis (built-in checks + cppcheck when present), modularity,
complexity (static inference; timing never proves Big-O), memory_safety
(ASan/UBSan), readability (LLM rubric, mean + 0.05 quantization,
`inconclusive` without gateway), similarity (shingle Jaccard, human-review
flag, no authorship claims — test-asserted). Bugs fixed: 3 real ones incl.
an incorrect renormalization and a nesting-depth miscount. **186 passed.**

## Phase 6 — Orchestration

Full `evaluate()` workflow (hash → validate → ingest → spec → generate →
oracle → compile → analyze → score → feedback → report → persist),
planner, bounded-parallel coordinator, append-only evidence registry,
feedback generation + deterministic verification (bounded retry, fallback;
feedback can never alter grade/evidence), CLI runner. Bugs fixed: a
Landlock/EAGAIN fork race (probe made fork-free), report/engine
reconciliation, dropped warnings. E2E with fake gateway: grade 95.3,
identical across repeated runs. **194 passed.**

## Phase 7 — Reports & frontend

FastAPI app (health, assignment/submission/rules uploads, evaluations
create/poll/report/history/artifacts), 5 Jinja2 pages + report fragment,
vanilla-JS polling, HTML report renderer (escaping, path redaction,
reconciling totals), background evaluations (2 workers), upload hardening
(size limits, UUID ids, artifact allowlist). Bugs fixed: 6 real ones
(template autoescape, SQLite threading, temp-file extensions, poll race,
lifespan). **203 passed.**

## Phase 8 — Verification & docs (this phase)

- Full suite **203 passed, 4 skipped, 0 failed** (skips: real-Ollama only,
  explicit reasons).
- Fixed a real hang: `test_unreachable_is_explicit` stalled because the
  mock server's `shutdown()` left its socket open (added `server_close()`).
- Variance: 5 runs → 95.3 ×5, **max deviation 0.00** (bar ≤ 7.0);
  `docs/VARIANCE.md`.
- Clean-env install: fresh venv + `pip install -e .` + 146 unit tests green.
- Real no-LLM demo via CLI on correct + buggy submissions; reports saved to
  `docs/demo/`; `docs/DEMO.md` (honest degraded path, `incomplete`/no grade).
- Acceptance walk: **20/22 PASS, 2 PASS-WITH-NOTE, 0 FAIL**
  (`docs/ACCEPTANCE.md`).
- Docs: README, INSTALL, OLLAMA, SANDBOX (updated), ARCHITECTURE, RULES,
  API, ANALYZERS, TESTING, TROUBLESHOOTING, LIMITATIONS, USE_CASES,
  TIMELINE, VARIANCE, DEMO, ACCEPTANCE.

## What's left

- **Real-Ollama validation** on a machine with Ollama + llama3.1:
  `pytest tests/integration/test_ollama_real.py`, test-generation quality
  review, and true LLM-driven variance measurement against the 7.0 bar.
- Optional: `agentic01.pdf` (plan/workload/timeline) still needs group
  member assignments — pending instructor-side input, not a code task.
