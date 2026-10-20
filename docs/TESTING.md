# Testing

## Suites

```bash
pytest tests/unit tests/integration tests/security   # everything
pytest tests/unit                                     # fast subset
pytest tests/integration                              # gateway, oracle, e2e (mock LLM)
pytest tests/security                                 # adversarial sandbox tests
```

Current: **348 passed, 4 skipped, 0 failed** (2026-10-20, this VM,
Python 3.12.3; the 4 skips are the environment-blocked real-Ollama
tests — see "What the skips mean").

| Suite | Covers |
|-------|--------|
| `tests/unit/` | rules validation/rejection, weights, scoring math + property sweeps, rounding, comparison policies, schemas, oracle provenance + the tier-3-never-trusted invariant, evidence immutability + blob integrity, state machine, persistence/recovery, ingestion, all 8 analyzers (real compile+run via sandbox), feedback verification |
| `tests/integration/` | Ollama gateway (mock server: retries, timeouts, validation retries, loopback enforcement), spec extraction, generation+oracle pipeline, full orchestration e2e (fake gateway), coordinator deadlines (hanging/crashing analyzers in real subprocesses), API endpoints + upload hardening, UI honesty, fresh-generation and concurrent-evaluation scenarios |
| `tests/security/` | Real hostile binaries, no enforcement mocks: read isolation (`/etc/shadow`, SSH keys, planted secrets), symlink escape, child identity (privilege drop), mandatory network isolation, seccomp denial, policy fail-closed, timeout/killpg tree kill, memory bomb, output flood, fork bomb, fs write escape, segfault, compile failures |

## Scenario coverage map

The governing spec names 18 acceptance scenarios. Each maps to concrete
tests — "covered" means a test in this repository exercises it;
"environment-blocked" means it cannot run on this machine and says so
loudly instead of passing.

| # | Scenario | Test(s) | Status |
|---|----------|---------|--------|
| 1 | Correct submission, trusted outputs | `integration/test_orchestration.py::test_full_workflow_fake_gateway`; `unit/test_analyzers_correctness.py` | Covered |
| 2 | Incorrect submission, trusted outputs | `unit/test_analyzers_correctness.py::test_buggy_program_scores_zero`; `integration/test_generation_oracle.py` | Covered |
| 3 | Compile failure | `security/test_compile_fail.py`; correctness no-binary path (`unit/test_analyzers_correctness.py`) | Covered |
| 4 | Timeout / process-tree kill | `security/test_timeout.py`; orphan-kill in `integration/test_coordinator_deadlines.py` | Covered |
| 5 | Memory / output exhaustion | `security/test_memory.py`; `security/test_output_flood.py` | Covered |
| 6 | Missing trusted oracle | correctness inconclusive tests (`unit/test_analyzers_correctness.py`); `integration/test_orchestration.py::test_degraded_no_gateway` | Covered |
| 7 | Ollama unavailable | `test_degraded_no_gateway` (no gateway → honest incomplete); gateway failure tests in `integration/test_ollama_gateway.py` | Covered |
| 8 | Missing required analyzer | `integration/test_orchestration.py::test_partial_analyzer_failure`; required-missing matrix in `unit/test_scoring_properties.py` | Covered |
| 9 | Missing optional analyzer, each policy | `unit/test_scoring_properties.py` (renormalize / exclude / zero / provisional × required / optional) | Covered |
| 10 | Invalid rules / weights | `unit/test_rules.py`; `integration/test_orchestration.py::test_invalid_rules_fail_fast` | Covered |
| 11 | Malformed / oversized uploads | `integration/test_api_hardening.py` (streaming 413, concurrency 429, temp cleanup); `integration/test_api.py` | Covered |
| 12 | Concurrent evaluations | `integration/test_fresh_generation.py::test_concurrent_evaluations_are_independent` | Covered |
| 13 | Report / score reconciliation | `unit/test_report.py` (`validate_report`); JSON↔HTML identity in `integration/test_ui_honesty.py` | Covered |
| 14 | Restart / persistence recovery | `unit/test_persistence.py` (`recover_interrupted_runs`); restart test in `integration/test_api_hardening.py` | Covered |
| 15 | Mandatory capability unavailable | `security/test_policy_fail_closed.py` (bogus mandatory capability → `SandboxUnavailable`; launcher failure → exit 125, binary never runs) | Covered (logic) — on a host genuinely lacking a capability the same fail-closed path refuses execution; that host state cannot be produced on this VM |
| 16 | Real Ollama integration | `integration/test_ollama_real.py` | **Environment-blocked here** — no Ollama on this VM; the tests skip with explicit reasons and must be run on the target machine (below) |
| 17 | Fresh generation across runs | `integration/test_fresh_generation.py::test_each_run_generates_fresh_tests` (shared gateway is re-called; run 2's evidence provably comes from run 2's response) | Covered |
| 18 | Feedback hallucination / contradiction | `integration/test_orchestration.py::test_feedback_verification_rejects_bad_evidence_id`; `unit/test_feedback_verification.py` (incl. out-of-range source lines, wrong metric scores); `unit/test_feedback_llm_check.py` | Covered |

## What the skips mean

The 4 skips are `tests/integration/test_ollama_real.py` — they require a
real Ollama server and **skip with explicit reasons** (never fake a pass):

```
SKIPPED: Ollama not reachable: cannot reach Ollama at http://127.0.0.1:11434
         ([Errno 111] Connection refused)
```

## Running the real-Ollama tests (target machine)

```bash
ollama serve
ollama pull llama3.1
pytest tests/integration/test_ollama_real.py -v
```

These verify: server reachability, model presence, structured generation
against the live model, and timeout behavior. They are the gate for
claiming real-LLM test-generation quality and true LLM variance (see
[docs/VARIANCE.md](docs/VARIANCE.md)).

## CI

`.github/workflows/ci.yml` runs three jobs on every push/PR: the
deterministic suite (`tests/unit tests/integration`), the security
suite as its own job (preceded by a capability-probe step so
skips/failures are explainable from the log; no `continue-on-error`
masking), and lint (`ruff check autograder/` under the basic ruleset
pinned in `pyproject.toml`, plus `compileall`).

## Mocks

Mocks live **only** under `tests/` and are clearly labeled (`MockGateway`,
`FakeGateway`, "TEST-ONLY"). No production module imports them. The
production `ReferenceSolutionOracle` returns honest `inconclusive` when its
executor isn't wired — it never pretends to have run anything.

## Fixtures

`tests/fixtures/c/` (correct/buggy/warning-laden/unsafe/loop-heavy C
programs) and `tests/fixtures/corpus/` are synthetic data. Production code
must not branch on fixture content.

## Current counts

Re-measured 2026-10-20 after the audit-remediation phases (this VM,
Python 3.12.3): **348 passed, 4 skipped, 0 failed** across
`tests/unit tests/integration tests/security`. Unit suite alone in a
clean venv installed purely from `requirements.lock`: **236 passed**
(see docs/INSTALL.md).
