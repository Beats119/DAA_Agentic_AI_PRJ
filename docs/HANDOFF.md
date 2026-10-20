# Final Handoff — AutoGrader

**Project:** AutoGrader — local, agentic evaluation of student C programs.
**Repo:** `Beats119/DAA_Agentic_AI_PRJ` (Phases 2–5 pushed; Phases 6–8
verified locally, pending push approval).
**Date:** 2026-10-20.

## What you have

A complete evaluation platform:

- **Web app** (FastAPI + Jinja2): dashboard, new-evaluation flow
  (assignment PDF + C source + rules), run detail with grade breakdown,
  rules management, operations page (Ollama + sandbox status).
- **Agentic workflow**: assignment PDF → spec extraction (explicit /
  inferred / ambiguous requirements with page provenance) → fresh LLM
  test generation every run → tiered oracle validation (Tier 1 reference,
  Tier 2 spec-derived, Tier 3 LLM-assisted; only `trusted` tests score)
  → sandboxed execution → 8 analyzers → deterministic weighted grade →
  evidence-verified feedback → JSON + HTML reports with full provenance.
- **Safety**: Landlock + network namespace + resource limits, fail-closed
  when isolation is unavailable; 19 adversarial tests (fork/memory/output
  bombs, fs escapes) contained.
- **Integrity**: no API keys, Ollama-only loopback inference, no
  assignment-specific hardcoded answers, no fabricated results; partial
  runs report `completed_with_warnings` / `incomplete`, never fake
  completeness.

## Exact commands

```bash
# Setup (once)
python3 -m venv .venv && .venv/bin/pip install -e .
ollama pull llama3.1            # on a machine with Ollama

# Web app
.venv/bin/python -m autograder.app.api.main --port 8000   # http://localhost:8000

# CLI evaluation
.venv/bin/python -m autograder.app.orchestration.run \
  --assignment assignment.pdf --source student.c --rules examples/rules/add.json

# Tests
.venv/bin/python -m pytest tests/unit tests/integration tests/security
```

## Test results (this VM, 2026-10-20)

- **203 passed, 4 skipped, 0 failed** (unit + integration + security).
- The 4 skips are the real-Ollama tests (`tests/integration/test_ollama_real.py`),
  skipped with explicit "not reachable" reasons — no Ollama on this VM.
- Repeated-run variance (deterministic path): grade 95.3 × 5 runs,
  **max deviation 0.00** (acceptance bar ≤ 7.0).
- Acceptance: **20/22 PASS, 2 PASS-WITH-NOTE, 0 FAIL** (`docs/ACCEPTANCE.md`).

## Known gaps (honest)

1. **Real-LLM validation pending** — test-generation quality and true
   LLM-driven variance must be measured on a machine with Ollama +
   llama3.1 (Khushi's WSL box). The wiring is proven via labeled mocks;
   the 4 real tests are ready to run there.
2. **cppcheck / valgrind / lizard / perf absent here** — those analyzers
   degrade to `unsupported` / built-in checks with recorded limitations.
3. **Push status**: Phases 6–8 code is verified locally but not yet on
   GitHub (push approvals were interrupted repeatedly by runtime
   restarts overnight, 2026-10-20).

## Docs map

- `docs/PLAN.md` — reconciled governing spec
- `docs/ARCHITECTURE.md` — components, contracts, oracle tiers
- `docs/INSTALL.md`, `docs/OLLAMA.md` — setup
- `docs/RULES.md`, `docs/API.md`, `docs/ANALYZERS.md` — references
- `docs/SANDBOX.md` — security boundary
- `docs/TESTING.md`, `docs/TROUBLESHOOTING.md` — operations
- `docs/LIMITATIONS.md` — 4 limitations with scenarios + mitigations
- `docs/USE_CASES.md` — 4 real-world use cases
- `docs/ACCEPTANCE.md`, `docs/VARIANCE.md`, `docs/TIMELINE.md`,
  `docs/DEMO.md` + `docs/demo/report_*.json` — verification records
