# AutoGrader

A locally-running, agentic evaluation platform for student-submitted C
programs. Give it an assignment specification (PDF), an instructor-written
grading rules file, and a student's C source — it generates candidate tests
with a local LLM (Ollama), validates them through a tiered oracle, executes
everything in an isolated Linux sandbox, and produces a deterministic,
evidence-grounded grade with JSON + HTML reports.

**No cloud inference, no API keys, no Streamlit.** Web UI is FastAPI +
server-rendered Jinja2 templates; every workflow is also a REST endpoint.

## Architecture

```
 Browser (Jinja2 pages + vanilla JS)
        |
 FastAPI app (autograder/app/api/)
   assignments / submissions / rules uploads
   evaluations (create, poll, report JSON/HTML, history, artifacts)
        |
 Orchestrator (autograder/app/orchestration/)
   hash inputs -> validate rules -> ingest PDF -> spec agent (LLM)
   -> test generation (LLM, once per assignment, bounded refinement;
      oracle-validated set cached per assignment and reused per student)
   -> tiered oracle (trusted/provisional/rejected/inconclusive)
   -> compile in sandbox -> analyzers (bounded parallel)
   -> deterministic scoring -> feedback (generate + verify)
   -> validate + persist report
        |
 +-- Ollama gateway (loopback-only): spec, test-gen, refinement,
 |      code review, feedback drafting
 +-- Secure sandbox: Landlock fs allowlist, mandatory netns, seccomp
 |      filter, rlimits, privilege drop, process-group kill — fail closed
 +-- 8 analyzers: correctness, compiler, static_analysis, modularity,
 |      complexity, memory_safety, readability, similarity
 +-- Evidence registry (typed, immutable) + SQLite index + artifacts dir
```

Key design rules: only `trusted`-oracle tests feed confirmed correctness;
scoring is a pure function of validated evidence (`grade = 100 × Σ w·s`);
the feedback model can never alter the grade; every degradation is an
explicit `unsupported`/`inconclusive`/`skipped`, never a silent zero.

Docs: [docs/PLAN.md](docs/PLAN.md) (governing spec) ·
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
[docs/SANDBOX.md](docs/SANDBOX.md) ·
[docs/API.md](docs/API.md) ·
[docs/RULES.md](docs/RULES.md) ·
[docs/ANALYZERS.md](docs/ANALYZERS.md) ·
[docs/INSTALL.md](docs/INSTALL.md) ·
[docs/OLLAMA.md](docs/OLLAMA.md) ·
[docs/TESTING.md](docs/TESTING.md) ·
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) ·
[docs/LIMITATIONS.md](docs/LIMITATIONS.md) ·
[docs/USE_CASES.md](docs/USE_CASES.md) ·
[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) ·
[docs/VARIANCE.md](docs/VARIANCE.md) ·
[docs/DEMO.md](docs/DEMO.md) ·
[docs/TIMELINE.md](docs/TIMELINE.md)

## Quickstart

```bash
# 1. Install (Python 3.11+, gcc required; deps pinned in requirements.lock)
pip install -r requirements.lock
pip install -e . --no-deps

# 2. Configure (optional)
cp .env.example .env   # OLLAMA_URL, ARTIFACT_DIR, AUTOGRADER_SANDBOX_POLICY

# 3. Start the web app
python -m autograder.app.api.main --port 8000
# -> http://127.0.0.1:8000  (Instructor / New evaluation / Rules / Operations)

# 4. Or run one evaluation from the CLI (no server needed)
python -m autograder.app.orchestration.run \
  --assignment assignment.pdf \
  --source student.c \
  --rules examples/rules/add.json
# with --no-llm for the degraded (no-model) path
```

Ollama setup (for the full LLM pipeline): install from
[ollama.com](https://ollama.com), then `ollama pull llama3.1`.
Details: [docs/OLLAMA.md](docs/OLLAMA.md). Without Ollama the system runs
in an honest degraded mode (correctness inconclusive, run `incomplete`).

## Project layout

```
autograder/app/
  api/            FastAPI app, Jinja2 templates, static assets
  core/           Pydantic schemas (rules, spec, tests, evidence, report)
  rules/          loading, validation, versioning, hashing
  persistence/    SQLite store + artifact layout
  scoring/        deterministic scoring engine (policy 1.1)
  reporting/      report schema validation / reconciliation
  ingestion/      assignment PDF/text ingestion
  specification/  spec-extraction agent
  ollama/         loopback-only gateway + health probes + prompt loader
  testing/        generation, candidate validation, tiered oracle
  sandbox/        capabilities probe, Landlock/netns/rlimit executor, compiler
  analyzers/      8 analyzers with a common typed contract
  orchestration/  planner, coordinator, workflow, evidence registry, feedback, CLI runner
configs/          models.yaml (task -> model), app settings
prompts/          versioned LLM prompt assets (v1.0)
examples/rules/   annotated example rules (add.json)
tests/            unit / integration / security (+ fixtures)
docs/             all documentation
```

## Testing

```bash
pytest tests/unit tests/integration tests/security   # full suite
pytest tests/unit                                     # fast subset
```

Current state: **348 passed, 4 skipped** (the 4 skips are real-Ollama
tests; they run on a machine with Ollama). See [docs/TESTING.md](docs/TESTING.md).

## Status

Phases 2–8 complete: foundations, local-AI integration, secure sandbox,
8 analyzers, orchestration, FastAPI UI, verification + docs. Remaining:
real-Ollama validation (test-generation quality, true LLM variance) on a
machine with Ollama installed. See [docs/TIMELINE.md](docs/TIMELINE.md) and
[docs/LIMITATIONS.md](docs/LIMITATIONS.md).
