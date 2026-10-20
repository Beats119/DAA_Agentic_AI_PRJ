# Final Handoff — AutoGrader

**Project:** AutoGrader — local, agentic evaluation of student C programs.
**Repo:** `Beats119/DAA_Agentic_AI_PRJ`
**Date:** 2026-10-20 (refreshed after the full repository audit and
remediation phases B–N; this document describes the current working
tree).

## What you have

A complete evaluation platform:

- **Web app** (FastAPI + Jinja2): dashboard, new-evaluation flow
  (assignment PDF + C source + rules), run detail with grade breakdown,
  rules management, operations page (Ollama + sandbox status). Uploads
  are streamed with incremental size enforcement (413 early, 429 over
  the concurrency bound); rules uploads work in JSON and YAML.
- **Agentic workflow**: assignment PDF → spec extraction (explicit /
  inferred / ambiguous requirements with page provenance) → fresh LLM
  test generation every run (token/execution budgets enforced) → tiered
  oracle validation (Tier 1 reference, Tier 2 spec-derived, Tier 3
  LLM-assisted; only `trusted` tests score; an unvalidated reference is
  capped at `provisional`) → sandboxed execution → 8 analyzers, each in
  its own subprocess under hard deadlines → deterministic weighted
  grade (scoring policy **1.1**: no eligible evidence → grade `None`,
  never a manufactured zero; configured *and* effective weights both
  reported) → evidence-verified feedback (source-location and
  metric-score claims are checked too) → JSON + HTML reports with full
  provenance.
- **Safety — sandbox 2.0**: a dedicated launcher process (no
  `preexec_fn`) applies, in order, a mandatory network namespace,
  rlimits, a privilege drop to uid 65534 when the app runs as root,
  `no_new_privs`, a narrow Landlock allowlist (run dir + system library
  dirs + `/dev/null` only), and a restrictive seccomp profile — then
  execs. Any mandatory step failing refuses the run (fail closed,
  `SandboxUnavailable`). 32 adversarial security tests with real
  hostile binaries (read isolation incl. `/etc/shadow` and SSH keys,
  symlink escape, fork/memory/output bombs, fs escapes, process-tree
  kills).
- **Integrity**: no API keys, Ollama-only loopback inference, no
  assignment-specific hardcoded answers, no fabricated results; partial
  runs report `completed_with_warnings` / `incomplete`, never fake
  completeness. SQLite runs in WAL mode; a restart fails interrupted
  runs explicitly instead of leaving them stuck.

## Exact commands

```bash
# Setup (once) — see docs/INSTALL.md for the full, verified sequence
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e . --no-deps
ollama pull llama3.1            # on a machine with Ollama

# Web app
.venv/bin/python -m autograder.app.api.main --port 8000   # http://localhost:8000

# CLI evaluation
.venv/bin/python -m autograder.app.orchestration.run \
  --assignment assignment.pdf --source student.c --rules examples/rules/add.json

# Tests
.venv/bin/python -m pytest tests/unit tests/integration tests/security
```

## Test results (this VM, 2026-10-20, post-remediation)

- **348 passed, 4 skipped, 0 failed** (unit + integration + security).
- The 4 skips are the real-Ollama tests
  (`tests/integration/test_ollama_real.py`), skipped with explicit
  "not reachable" reasons — no Ollama on this VM.
- Repeated-run variance (deterministic FakeGateway path): grade 95.3 ×
  5 runs, **max deviation 0.00** (acceptance bar ≤ 7.0). Real-model
  variance is **UNVERIFIED** — see below.
- Acceptance: 16 VERIFIED · 3 PARTIAL · 1 MOCK-TESTED ONLY ·
  2 BLOCKED–ENVIRONMENT · 0 NOT IMPLEMENTED (`docs/ACCEPTANCE.md`).
- Audit: all 28 registered defects (D1–D28) resolved
  (`docs/AUDIT.md`, Resolution section).

## Known gaps (honest)

1. **Real-LLM validation pending (the big one)** — test-generation
   quality, real readability reviews, and true LLM-driven variance
   vs the ≤ 7.0 bar must be measured on a machine with Ollama +
   llama3.1 (Khushi's WSL box). The wiring is proven via labeled
   mocks; the gate is `tests/integration/test_ollama_real.py` plus
   the 5-run procedure in `docs/OLLAMA.md`. Acceptance items #5/#17
   stay BLOCKED–ENVIRONMENT until then.
2. **cppcheck / valgrind / lizard / perf absent here** — static
   analysis runs its built-in checks and routes cppcheck through the
   sandbox when present, but that path is unexercised on this VM;
   other optional tools degrade with recorded limitations.
3. **cgroup v2 not delegated** — resource *containment* is enforced
   (rlimits, wall timeout, process-group kills) and tested, but there
   is no cgroup-level aggregate accounting (`docs/LIMITATIONS.md` #6).
4. **CI unverified** — `.github/workflows/ci.yml` exists and is
   YAML-validated, but its first GitHub Actions run has not been
   observed.
5. **16 GB target** — supported by measured RSS (≈61 MB idle API,
   ≈178 MB peak per evaluation) times the configured concurrency
   bounds, not by a load test on a 16 GB machine.

## Docs map

- `docs/PLAN.md` — reconciled governing spec
- `docs/AUDIT.md` — full repository audit (pre-fix record) + resolution
  of all 28 defects
- `docs/ARCHITECTURE.md` — components, contracts, oracle tiers
- `docs/INSTALL.md`, `docs/OLLAMA.md` — setup (+ the real-model
  verification gate in OLLAMA.md)
- `docs/RULES.md`, `docs/API.md`, `docs/ANALYZERS.md` — references
- `docs/SANDBOX.md` — security boundary (sandbox 2.0)
- `docs/TESTING.md`, `docs/TROUBLESHOOTING.md` — operations
- `docs/LIMITATIONS.md` — limitations with scenarios + mitigations
- `docs/USE_CASES.md` — 4 real-world use cases
- `docs/ACCEPTANCE.md`, `docs/VARIANCE.md`, `docs/TIMELINE.md`,
  `docs/DEMO.md` + `docs/demo/report_*.json` — verification records
