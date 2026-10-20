# Ollama Setup & Model Configuration

AutoGrader uses **only** a local Ollama server for every LLM task. No cloud
inference, no API keys, no exceptions — the gateway (`autograder/app/ollama/`)
refuses non-loopback URLs at construction time.

## Install & models

```bash
# https://ollama.com/download/linux
ollama serve
ollama pull llama3.1      # ~4.9 GB; default for every task
ollama list               # verify it's there
```

The app checks availability at startup and on the **Operations** page
(`/operations`) and `/health`: reachable models, recommended small models,
and an actionable error naming the exact `ollama pull <model>` command when
a configured model is missing.

## Per-task model config (`configs/models.yaml`)

```yaml
tasks:
  test_generation: {model: "llama3.1", temperature: 0, seed: 42,
                    timeout_s: 180, max_tokens: 4096}
  test_refinement: {model: "llama3.1", temperature: 0, seed: 42, ...}
  specification:   {model: "llama3.1", temperature: 0, seed: 42, ...}
  code_review:     {model: "llama3.1", temperature: 0, seed: 42, ...}
  feedback:        {model: "llama3.1", temperature: 0, seed: 42, ...}
  feedback_check:  {model: "llama3.1", temperature: 0, seed: 42, ...}
```

Temperature 0 + fixed seed keep repeated runs as stable as the model
allows. To try a different local model, change the `model` value — the
loader rejects any task entry that does not name its model, so the model
for every task comes from this file. Budgets (timeouts, token caps) live here
and in the rules file's `test_generation` section.

## What happens when Ollama is unreachable

The workflow **does not fail**. The runner probes first (`make_gateway`);
on failure it proceeds with `gateway=None` and records the limitation:

- spec extraction + test generation skipped → correctness `inconclusive`
- readability review `inconclusive` (deterministic analyzers still run)
- feedback rendered by the deterministic evidence-based fallback
- required metric missing → run status `incomplete`, grade `None`

This is the honest degraded path — demonstrated in [docs/DEMO.md](docs/DEMO.md).
Nothing is fabricated to fill the gap.

## Real-model verification (acceptance gate)

**Status: NOT YET RUN.** Every LLM-driven behavior in this project has
been proven only against labeled test doubles on the development VM
(which has no Ollama). The procedure below is the acceptance gate for
the BLOCKED–ENVIRONMENT criteria in `docs/ACCEPTANCE.md` (#5 fresh
real-LLM generation, #17 real-model variance; it also closes the
reachable-path gap in #3 and lets #22 be re-walked). Run it on the
target machine (WSL, Ollama installed, `llama3.1:latest` pulled):

```bash
# 1. Start the server and confirm the model is present
ollama serve
ollama list                      # must show llama3.1

# 2. Run the real-model test gate (4 tests: reachability, tags
#    roundtrip, model usable for test generation, structured
#    generation against the live model). On a machine without Ollama
#    these skip with explicit reasons — a skip there is NOT a pass.
.venv/bin/python -m pytest tests/integration/test_ollama_real.py -v

# 3. Measure REAL repeated-run variance: evaluate the same submission
#    5 times WITHOUT --no-llm (real gateway), distinct run ids
for i in 0 1 2 3 4; do
  .venv/bin/python -m autograder.app.orchestration.run \
    --assignment docs/demo/assignment_add.txt \
    --source tests/fixtures/c/correct_add.c \
    --rules examples/rules/add.json \
    --run-id realvar_$i --base-dir /tmp/realvar
done

# 4. Compare the 5 grades: max deviation must be ≤ 7.0
python3 -c "
import json, glob
grades = [json.load(open(p))['grade']
          for p in sorted(glob.glob('/tmp/realvar/runs/*/report.json'))]
print('grades:', grades)
print('max deviation:', max(grades) - min(grades) if grades and None not in grades else 'n/a — a run produced no grade; read its report limitations first')
"
```

Record the outcome — the 4 test results, the 5 grades, and the max
deviation — in `docs/VARIANCE.md`, and update the affected rows in
`docs/ACCEPTANCE.md`. Until this has been done, real-model generation
quality and variance remain **UNVERIFIED**, regardless of mock-path
results.

## GPU notes

Ollama uses the GPU automatically when available (llama3.1 runs
comfortably on a mid-range NVIDIA card). CPU-only works too, just slower —
raise the per-task `timeout_s` values if generation times out on CPU.
Target machine profile in `configs/models.yaml` comments assumes 16 GB RAM.

## Prompts

All LLM prompts are versioned project assets under `prompts/` (front-matter:
version, task, output contract). The gateway records the prompt version and
hash in every `GenerationRecord`, so a report always says exactly which
prompt produced which output.
