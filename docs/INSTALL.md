# Installation & Setup (Linux)

## Prerequisites

- **Python 3.11 or newer** — the floor declared in `pyproject.toml`
  (`requires-python = ">=3.11"`); developed and verified on Python 3.12.
- **gcc** (or clang) for compiling student C programs (`gcc --version`).
  Required: without a compiler no submission can be built.
- **pip** (for the install sequence below).

Optional but recommended:

| Tool | What degrades without it |
|------|--------------------------|
| Ollama + a model (e.g. `llama3.1`) | LLM spec extraction, test generation, readability review, LLM feedback — the run takes the honest degraded path (`incomplete`, correctness `inconclusive`) |
| `cppcheck` | static_analysis uses built-in checks only (stated in the report) |
| `valgrind` | not currently wired; memory safety uses ASan/UBSan via gcc |
| `lizard` / `perf` | not required; modularity uses built-in parsing, complexity uses static inference + timing |

Install cppcheck on Debian/Ubuntu if you want it: `sudo apt install cppcheck`.

The sandbox additionally needs a Linux kernel with Landlock, network
namespaces, seccomp and rlimits — see the capability check under
[Sandbox identity & policy](#sandbox-identity--policy) below and
[docs/SANDBOX.md](SANDBOX.md).

## Install

Dependencies are pinned in `requirements.lock` (exact versions, no local
paths, no editable self-reference). Install the lock first, then the
project itself:

```bash
git clone <repo-url> && cd <repo>
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock
pip install -e . --no-deps
```

The lock also pins the dev tools (`pytest`, `ruff`), so the test suite
and the linter work from the same environment with no extra installs.

Verified 2026-10-20 on a clean venv (Python 3.12.3), using exactly the
sequence above: `pytest tests/unit` → **236 passed**.

## Configure

```bash
cp .env.example .env
```

`.env.example` lists exactly the environment variables the code reads
(grep-verified):

| Variable | Default | Meaning |
|----------|---------|---------|
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Local Ollama endpoint. Must be loopback — the gateway refuses anything else. |
| `ARTIFACT_DIR` | `./artifacts` | Where run artifacts and the SQLite run database are stored (web app launcher; the CLI takes explicit paths instead). |
| `AUTOGRADER_SANDBOX_POLICY` | *(unset)* | Path to a JSON sandbox-policy file (see below). Unset = built-in policy with all mandatory enforcement on. An invalid file is an error, never a silent default. |
| `AUTOGRADER_FEEDBACK_LLM_CHECK` | off | `1/true/yes/on` enables the supplementary LLM feedback audit on top of the authoritative deterministic verifier. |

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

## Sandbox identity & policy

Student code never runs as the user who started the app **when the app
runs as root**: the sandbox launcher drops privileges to a dedicated
unprivileged identity before `exec`. By default that identity is
`nobody`/`nogroup` (uid/gid 65534). When the app runs as a normal user,
the child keeps that user's uid (there is no other uid to drop to
without root) and is contained by Landlock + seccomp + network
namespace; the enforcement report in every run states which case
applied (`uid_dropped: true/false`).

For a root-run deployment (e.g. a systemd service) you can provision a
dedicated identity instead of `nobody`:

```bash
sudo useradd --system --no-create-home autograder-sandbox
id autograder-sandbox          # note the uid/gid, e.g. 998/998
```

then point the policy at it with a JSON policy file:

```json
{ "sandbox_uid": 998, "sandbox_gid": 998 }
```

```bash
export AUTOGRADER_SANDBOX_POLICY=/path/to/sandbox-policy.json
```

The same file can adjust the other `SandboxPolicy` fields (`mandatory`,
`fs_read_paths`, `seccomp_profile`, `drop_privileges_when_root`) — see
`autograder/app/sandbox/policy.py`. The application itself never needs
root for normal operation; root only matters because the *child* then
has something to drop from.

**Startup capability check.** The sandbox probes the host before any
student code runs:

```bash
python -m autograder.app.sandbox.capabilities
```

prints the measured capabilities (network namespace, seccomp filter,
Landlock ABI, cgroup v2, rlimits, compiler). On top of the probes,
`SandboxExecutor.ensure_available()` runs a functional self-test (a real
sandboxed `cat` round-trip through the launcher) before the first
execution of a run. If any **mandatory** mechanism (resource limits,
Landlock filesystem containment, network namespace, seccomp filter,
`no_new_privs`) is unavailable, execution is refused with
`SandboxUnavailable` — the evaluation fails closed instead of running
student code uncontained. See [docs/SANDBOX.md](SANDBOX.md).

## Resource bounds (16 GB target)

Configured concurrency bounds (all in code, not aspirational):

| Bound | Value | Where |
|-------|-------|-------|
| Background evaluations in the web app | 2 workers | `api/app.py` (`ThreadPoolExecutor(max_workers=2)`) |
| Analyzer worker processes per evaluation | ≤ 4 | coordinator `max_parallel=4` |
| Concurrent uploads | ≤ 4 | upload guard (`_MAX_CONCURRENT_UPLOADS = 4`), excess → 429 |
| Per-test sandbox limits | from the rules file | e.g. `examples/rules/add.json`: 256 MB, 2.0 s wall, 32 processes, 1 MB output per test |

Measured on this development VM (Python 3.12.3, 2026-10-20; peak RSS of
the whole process tree, sampled via `/proc`):

| Measurement | Peak RSS |
|-------------|----------|
| Web app server, idle (after `/health`) | ≈ 61 MB |
| One full CLI evaluation, degraded/no-LLM path (`--no-llm`, example rules) | ≈ 178 MB |

Arithmetic from those measurements: app server (61 MB) + 2 concurrent
evaluations (~178 MB each, a figure that already includes up to 4
analyzer workers) ≈ **0.5 GB**, plus transient sandboxed test children
bounded by the rules' per-test memory limit (256 MB each in the example
rules). Ollama's model memory is separate and depends on the model
(`llama3.1` ≈ 4.9 GB on disk; Ollama holds the weights in its own
process).

**Honesty note:** a full load test on an actual 16 GB machine has NOT
been performed. The claim made here is only the arithmetic above —
measured per-unit RSS × the configured concurrency bounds — not a
verified 16 GB compatibility certification.

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
