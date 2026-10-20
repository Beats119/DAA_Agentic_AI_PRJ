# Troubleshooting

## Ollama unreachable / all runs `incomplete`

**Symptom:** `/health` shows `ollama.reachable: false`; reports say "no LLM
gateway for this run".
**Fix:** `ollama serve`, then `ollama pull llama3.1`. Check `OLLAMA_URL` in
`.env` (must be loopback — the gateway refuses anything else). This is the
honest degraded path, not a bug: deterministic analyzers still run, and the
report states exactly what is missing. See [docs/OLLAMA.md](docs/OLLAMA.md).

## `SandboxUnavailable` / evaluations refuse to execute

**Symptom:** `SandboxError: ...` or `ensure_available` failures.
**Fix:** the host can't provide the minimum isolation bar (rlimits or
process-group kill). Check `/health` → `sandbox`. Don't work around it with
`SANDBOX_POLICY=permissive` except for debugging — permissive mode exists
for diagnostics, not for grading untrusted code. See
[docs/SANDBOX.md](docs/SANDBOX.md).

## Port already in use

**Symptom:** uvicorn fails to bind.
**Fix:** `python -m autograder.app.api.main --port 8001` (any free port).

## Rules upload returns 422

**Symptom:** `POST /api/rules` → `{"detail": "invalid rules: ..."}`.
**Fix:** read the message — common causes: metric weights don't sum to 1.0
(within 1e-9), unknown `analyzer` id, negative limits, bad thresholds.
Compare against `examples/rules/add.json`. Full reference:
[docs/RULES.md](docs/RULES.md).

## A run is stuck in a non-terminal state

**Symptom:** `/api/evaluations/{id}` shows `executing` for a long time.
**Fix:** the workflow wraps every stage in timeouts; a crashed worker is
recorded as `failed` with diagnostics. Check the API server logs and
`artifacts/runs/<run_id>/`. The API pool is bounded (2 concurrent
evaluations); extra runs queue.

## LLM output fails validation repeatedly

**Symptom:** limitations mention validation retries exhausted.
**Fix:** the model may be too small or the prompt too strict. Check the
`GenerationRecord`s in the report (model id, prompt version, attempts).
Options: pull a larger local model and point `configs/models.yaml` at it,
or simplify the prompt asset in `prompts/` (bump its version).

## Report totals don't reconcile

**Symptom:** `validate_report` error.
**Fix:** this is a bug, not a config issue — the invariant
Σ contributions == grade (within 0.05) is asserted by tests. Report it with
the run's `manifest.json`.

## Tests hang

A past cause: `test_unreachable_is_explicit` stalled because the mock
server's `shutdown()` left the listening socket open (fixed with
`server_close()`; see [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)). If a new
hang appears, run the suspect file alone with `timeout` to isolate it.
