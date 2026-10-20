# API Reference

Base URL: `http://127.0.0.1:8000` (see `--port`). All evaluation workflows
are available via the API; the HTML pages are thin views over the same
endpoints. Uploaded files get server-generated UUID names — no client
filenames are trusted.

## Pages (server-rendered Jinja2 + vanilla JS)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Dashboard: recent runs, quick links |
| GET | `/new` | New evaluation form (assignment + submission + rules pickers, creates via API, polls status) |
| GET | `/runs/{run_id}` | Run detail: status, grade, metrics, evidence, links to reports |
| GET | `/rules` | Rules registry: list uploaded/seeded rules with versions + hashes |
| GET | `/operations` | Ollama reachability + models, sandbox capabilities, app/tool versions |

## Endpoints

### `GET /health`
Liveness + dependency honesty. Returns:
```json
{"status": "ok",
 "ollama": {"reachable": false, "models": [], "recommended": [],
            "error": "cannot reach Ollama at ... (Connection refused)"},
 "sandbox": {"resource_limits": true, "network_namespace": true,
             "landlock_abi": 1, "compiler": "/usr/bin/gcc", ...},
 "versions": {"scoring_policy": "1.0", "sandbox": "sandbox-1.0",
              "python": "3.12.3", "app": "0.1.0"}}
```

### `POST /api/assignments` → 201
Multipart `file` (PDF or plain text, ≤ 20 MB). Returns
`{id, sha256, size, pages}` (pages for PDFs). 400 on non-PDF/non-text,
413 on oversize.

### `POST /api/submissions` → 201
Multipart `file` (UTF-8 C source, ≤ 1 MB, non-empty). Returns `{id, sha256,
size}`. 400 on empty/non-UTF-8, 413 on oversize.

### `GET /api/rules` → 200
List of rules `{id, version, hash, metrics:[names], ...}` (seeded from
`examples/rules/` on first start, plus uploads).

### `POST /api/rules` → 201
Multipart `file` (JSON/YAML, ≤ 512 KB). The file is **validated** before
storage; returns `{id, version, hash, metrics}`. 422 on invalid rules.

### `POST /api/evaluations` → 202
Body: `{"assignment_id": "...", "submission_id": "...", "rules_id": "..."}`.
Validates the three ids (404 on unknown) and the rules (422 on invalid),
then queues the run on a bounded worker pool (max 2 concurrent). Returns
`{"run_id": "...", "status": "queued"}`.

### `GET /api/evaluations/{run_id}` → 200
Run state: `{"run_id", "state", ...}`. 400 on malformed run id, 404 on
unknown run.

### `GET /api/evaluations/{run_id}/report` → 200
The validated JSON report (reconciled against the scoring engine). 404 if
the run hasn't produced one yet.

### `GET /api/evaluations/{run_id}/report.html` → 200
The same report as a self-contained HTML page (student source escaped,
server paths redacted).

### `GET /api/history?limit=50` → 200
Recent runs (1–200) from the SQLite index.

### `GET /api/artifacts/{run_id}/{name}` → 200 / 404
Allowlisted artifacts only (`report.json`, `report.html`, `evidence.json`,
`manifest.json`, `source.c`). 400 on malformed run id, 404 on
non-allowlisted names — no path traversal.

## Errors

Standard codes: 400 (bad input/id), 404 (unknown id/artifact), 413
(oversize upload), 422 (invalid rules), 500 (unexpected — logged, never
leaks server paths or tracebacks to the client).
