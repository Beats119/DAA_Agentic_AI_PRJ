# Testing

## Suites

```bash
pytest tests/unit tests/integration tests/security   # everything
pytest tests/unit                                     # fast subset (~3 s)
pytest tests/integration                              # gateway, oracle, e2e (mock LLM)
pytest tests/security                                 # adversarial sandbox tests
```

Current: **203 passed, 4 skipped, 0 failed** (2026-10-20).

| Suite | Covers |
|-------|--------|
| `tests/unit/` | rules validation/rejection, weights, scoring math, rounding, comparison policies, schemas, oracle provenance + the tier-3-never-trusted invariant, evidence immutability, state machine, ingestion, all 8 analyzers (real compile+run via sandbox) |
| `tests/integration/` | Ollama gateway (mock server: retries, timeouts, validation retries, loopback enforcement), spec extraction, generation+oracle pipeline, full orchestration e2e (fake gateway), FastAPI endpoints |
| `tests/security/` | 11 modules, real hostile binaries: timeout/killpg, memory bomb, output flood, fork bomb, fs escape, segfault, compile failures, network claim honesty, fail-closed, capabilities, oracle-under-sandbox |

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

## Mocks

Mocks live **only** under `tests/` and are clearly labeled (`MockGateway`,
`FakeGateway`, "TEST-ONLY"). No production module imports them. The
production `ReferenceSolutionOracle` returns honest `inconclusive` when its
executor isn't wired — it never pretends to have run anything.

## Fixtures

`tests/fixtures/c/` (correct/buggy/warning-laden/unsafe/loop-heavy C
programs) and `tests/fixtures/corpus/` are synthetic data. Production code
must not branch on fixture content.
