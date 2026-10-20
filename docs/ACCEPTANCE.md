# Acceptance Checklist

Walked against `docs/PLAN.md` §6 (22 items), 2026-10-20, this VM
(Python 3.12.3, gcc 13.3, **no Ollama**, no cppcheck/valgrind/lizard/perf).

**Tally: 20 PASS, 2 PASS-WITH-NOTE, 0 FAIL.**

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | App starts locally with a usable web UI | **PASS** | `python -m autograder.app.api.main --port 8931`; `/health` returns honest Ollama+sandbox+version status; pages `/`, `/new`, `/rules`, `/operations` all 200. |
| 2 | No external inference services; no API keys | **PASS** | `OllamaClient` rejects non-loopback URLs at construction (`tests/integration/test_ollama_gateway.py::test_loopback_enforcement`); no `Authorization` header exists anywhere in `autograder/`; no key config in `.env.example`. |
| 3 | Ollama + model availability checked at startup and operations page | **PASS** | `/health` reports `ollama.reachable/models/recommended/error` (verified live: "Connection refused" surfaced, not hidden); `/operations` page renders the same; `ensure_model()` names the exact `ollama pull <model>` command when missing. |
| 4 | Rules editable without code changes | **PASS** | `POST /api/rules` validates and stores versioned rules; `rules/` page lists them; `examples/rules/add.json` demonstrates all sections; invalid rules rejected with 422 (`test_api.py`). |
| 5 | Fresh, real LLM test generation on every evaluation | **PASS-WITH-NOTE** | Workflow calls `generate_test_suite()` every run; no test/grade cache exists in the codebase (verified by inspection). *Note: real-LLM generation quality not measured here — no Ollama on this VM; integration tests prove the wiring with a labeled mock.* |
| 6 | Generated tests validated before execution | **PASS** | Pipeline order in `workflow.py`: `validate_candidate` → oracle assessment → compile → run only `trusted` tests. `test_stats.validated/executed` recorded in every report. |
| 7 | Untrusted C runs in verified isolation | **PASS** | `tests/security/`: 19 tests with real hostile binaries — timeout+killpg tree kill, memory bomb (RLIMIT_AS), output flood cap, fork bomb (RLIMIT_NPROC), fs escape blocked (Landlock), segfault recorded, fail-closed on degraded host. |
| 8 | Timeouts / resource exhaustion handled | **PASS** | Same suite; `ExecutionResult.timed_out`, signals, and truncation flags recorded per run, never a hang. |
| 9 | Analyzer results use structured schemas + evidence references | **PASS** | `AnalyzerResult` (Pydantic, `extra="forbid"`) with `evidence_refs`, `findings`, `execution_metadata`; evidence registry append-only, immutable after finalize (`test_persistence.py`). |
| 10 | Score computed by deterministic software, never chosen by the model | **PASS** | `autograder/app/scoring/engine.py` is a pure function with no gateway dependency; feedback module cannot touch grade/evidence (enforced by code + test). |
| 11 | Identical validated evidence + rules → identical score | **PASS** | Variance runs: 5/5 identical grades (95.3), deviation 0.00 (`docs/VARIANCE.md`). |
| 12 | Unverified expected outputs never become confirmed failures | **PASS** | Tier-3 agreement capped at `provisional` by code assertion (`strategies.py`) + dedicated test (`test_oracle_tier3_agreement_never_trusted`); only `trusted` tests feed the correctness score. |
| 13 | Missing required analyzers can never silently score full marks | **PASS** | `uncertainty_policy.required_metric_missing: "incomplete"` → grade `None`, status `incomplete` (demonstrated live in `docs/demo/`); optional metrics renormalize only when policy says so, always recorded. |
| 14 | JSON and HTML reports reconcile with the computed score | **PASS** | `validate_report()` asserts Σ contributions == grade within 0.05 + per-metric `100·w·s` + policy version; `/api/evaluations/{id}/report` and `/report.html` serve the same validated report (`test_api.py`). |
| 15 | Findings traceable to evidence | **PASS** | Every report carries `evidence_coverage`, per-finding evidence refs, tool/model/prompt versions, and `reproduction` info (input hashes, run id). |
| 16 | Similarity never claims cheating/authorship proof | **PASS** | Module docstring + report language: "flag for human review"; a unit test asserts the report text contains no authorship claim. |
| 17 | Repeated-run stability measured and reported | **PASS-WITH-NOTE** | Measured: max deviation 0.00 ≤ 7.0 (`docs/VARIANCE.md`). *Note: measured with deterministic fake LLM; real-LLM variance measurement belongs on the Ollama machine.* |
| 18 | Tests run with accurate pass/fail/skip reporting | **PASS** | Full suite: **203 passed, 4 skipped, 0 failed**; skips are exactly the 4 real-Ollama tests with explicit reasons (never faked). |
| 19 | Real end-to-end demonstration documented | **PASS** | `docs/DEMO.md` + real `report_*.json` from the CLI; degraded no-LLM path stated plainly; full pipeline proven by integration e2e tests + variance runs. |
| 20 | Security boundary + missing capabilities documented | **PASS** | `docs/SANDBOX.md`: enforcement table, capability probe results, fail-closed rules, residual risks. |
| 21 | Setup instructions, example rules, final-report material | **PASS** | `docs/INSTALL.md`, `docs/OLLAMA.md`, `examples/rules/add.json`, `docs/USE_CASES.md`, `docs/LIMITATIONS.md`, `docs/TIMELINE.md`. |
| 22 | Nothing mocked presented as implemented | **PASS** | Mocks exist only under `tests/` (labeled `Mock`/`Fake`, test-only); production `ReferenceSolutionOracle` returns honest `inconclusive` until wired; degraded paths report `unsupported`/`inconclusive`/`skipped`, never zeros. |

## Fixed during this phase

- `tests/integration/test_ollama_gateway.py::test_unreachable_is_explicit`
  hung the suite: `server.shutdown()` stops the serve loop but leaves the
  listening socket open, so the client connection was accepted and stalled
  until the 120 s default timeout. Fixed with `server.server_close()` —
  the test now finishes in milliseconds (file: 6.7 s total).
