# Demo: End-to-End Evaluation Runs

Two real evaluation runs executed 2026-10-20 on this VM via the CLI.
No outputs were hand-written — both `report_*.json` files are the actual
artifacts produced by the code.

## What was run

```bash
python -m autograder.app.orchestration.run \
  --assignment docs/demo/assignment_add.txt \
  --source tests/fixtures/c/correct_add.c \   # and buggy_add.c
  --rules examples/rules/add.json \
  --no-llm --run-id demo_correct --base-dir docs/demo/artifacts
```

- **Assignment:** `docs/demo/assignment_add.txt` — synthetic "add two integers"
  spec with a worked example (2 3 → 5).
- **Rules:** `examples/rules/add.json` (8 metrics, weights sum to 1.0).
- **Submissions:** `tests/fixtures/c/correct_add.c` (correct) and
  `tests/fixtures/c/buggy_add.c` (off-by-one: `a + b + 1`).
- **Mode:** `--no-llm` — Ollama is not installed on this VM, so the workflow
  took the degraded path honestly (see below).

## Results

| Run | Submission | Status | Grade |
|-----|-----------|--------|-------|
| `demo_correct` | correct_add.c | `incomplete` | None |
| `demo_buggy` | buggy_add.c | `incomplete` | None |

Full reports: `docs/demo/report_correct.json`, `docs/demo/report_buggy.json`
(raw run artifacts under `docs/demo/artifacts/runs/<run_id>/`).

Per-analyzer statuses (correct run):

| Analyzer | Status | Note |
|----------|--------|------|
| correctness | `inconclusive` | required metric; no trusted tests without LLM |
| compiler | `completed` | clean compile |
| static_analysis | `completed` | built-in checks (cppcheck absent — stated) |
| modularity | `completed` | |
| complexity | `completed` | static leg; timing caveat recorded |
| memory_safety | `inconclusive` | needs trusted tests to execute |
| readability | `inconclusive` | needs the local model |
| similarity | `skipped` | empty corpus — stated, not faked |

16 uncertainty entries and 7 limitations are recorded in the report, e.g.
"no LLM gateway for this run: specification extraction and test generation
skipped; correctness will be inconclusive" and "required metric
'correctness' unavailable (status=inconclusive, score=None); marking
evaluation incomplete".

## What's real vs what's not in this demo

- **Real:** PDF/text ingestion, rules validation + hashing, sandboxed
  compilation, all deterministic analyzers, evidence registry, deterministic
  scoring, report validation, artifact persistence, the degraded no-LLM path.
- **Not exercised here:** LLM spec extraction, LLM test generation, tiered
  oracle on LLM candidates, LLM readability review, LLM feedback drafting.
  Those paths are covered by integration tests with a labeled mock gateway
  (`tests/integration/`), and by the variance runs in `docs/VARIANCE.md`.
  On a machine with Ollama running, drop `--no-llm` and the same command
  exercises the full pipeline.

## Reading the outcome honestly

Both runs report `incomplete` with no grade — that is the *correct*
behavior, not a failure of the demo. With no trusted oracle evidence, the
system refuses to manufacture a correctness verdict ("never silently award
full marks or invent evidence"). The buggy submission is therefore not
"failed" either: without a trustworthy oracle there is no confirmed basis
for failing it, and the report says exactly that.
