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
allows. To try a different local model, change the `model` value — model
names are never hardcoded in code. Budgets (timeouts, token caps) live here
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
