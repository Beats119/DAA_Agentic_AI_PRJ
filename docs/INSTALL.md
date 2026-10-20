# Installation & Setup (Linux)

## Prerequisites

- **Python 3.11+** (`python3 --version`)
- **gcc** (or clang) for compiling student C programs (`gcc --version`)
- **pip**

Optional but recommended:

| Tool | What degrades without it |
|------|--------------------------|
| Ollama + a model (e.g. `llama3.1`) | LLM spec extraction, test generation, readability review, LLM feedback — the run takes the honest degraded path (`incomplete`, correctness `inconclusive`) |
| `cppcheck` | static_analysis uses built-in checks only (stated in the report) |
| `valgrind` | not currently wired; memory safety uses ASan/UBSan via gcc |
| `lizard` / `perf` | not required; modularity uses built-in parsing, complexity uses static inference + timing |

Install cppcheck on Debian/Ubuntu if you want it: `sudo apt install cppcheck`.

## Install

```bash
git clone <repo-url> && cd <repo>
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Verified 2026-10-20: fresh venv + `pip install -e .` + `pytest tests/unit`
→ **146 passed**.

## Configure

```bash
cp .env.example .env
```

| Variable | Default | Meaning |
|----------|---------|---------|
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama endpoint. Must be loopback — the gateway refuses anything else. |
| `ARTIFACT_DIR` | `./artifacts` | Immutable per-run artifacts (`runs/<run_id>/`). |
| `DB_PATH` | `./autograder.db` | SQLite run index. |
| `SANDBOX_POLICY` | `strict` | `strict` fails closed when isolation is unavailable; `permissive` is for debugging only. |

There are no API keys anywhere in the project — all inference is local.

## Run

```bash
# Web app
python -m autograder.app.api.main --port 8000
# -> http://127.0.0.1:8000

# Single evaluation (CLI, no server)
python -m autograder.app.orchestration.run \
  --assignment assignment.pdf --source student.c \
  --rules examples/rules/add.json [--no-llm]
```

## Ollama (for the full pipeline)

```bash
# Install from https://ollama.com/download/linux, then:
ollama serve            # usually auto-started
ollama pull llama3.1    # ~4.9 GB; the default per-task model
```

See [docs/OLLAMA.md](docs/OLLAMA.md) for per-task model config and GPU notes.

## Sandbox notes

On first run the app probes Linux capabilities (user/network namespaces,
seccomp, cgroup v2, rlimits, Landlock, compiler) and records them in
`/health` and every report. If the minimum isolation bar can't be met, the
sandbox **refuses to execute** (`SandboxUnavailable`) instead of running
student code uncontained. See [docs/SANDBOX.md](docs/SANDBOX.md).

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
