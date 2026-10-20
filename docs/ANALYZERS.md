# Analyzers

Eight analyzers share one contract (`autograder/app/analyzers/base.py`):
`analyze(ctx: AnalyzerContext) -> AnalyzerResult`, where `AnalyzerContext`
carries `source_path`, compiled `binary_path`s, the `RuleSet`, the metric's
config, run directories, the sandbox executor/compiler, and an optional LLM
gateway. Every analyzer declares its version (`<name>-1.0`) and returns an
honest status: `completed` / `failed` / `skipped` / `unsupported` /
`inconclusive`. Analyzer-specific tunables live in `metric.params`;
defaults are in each module's `DEFAULTS` dict — never magic numbers inline.

| Analyzer | Measures | Scoring | Honest statuses |
|----------|----------|---------|-----------------|
| `correctness` | Runs `trusted`-oracle tests against the student binary in the sandbox; compares via the rules' `comparison_policy` | `passed_trusted / total_trusted`. **Zero trusted tests → `inconclusive`, not 0.0.** Provisional/inconclusive tests may run for exploration but never enter the score. Student compile failure → `failed` with diagnostics. | `inconclusive` (no trusted tests), `failed` (no binary) |
| `compiler` | Compilation diagnostics quality | Clean = 1.0; each warning −`warning_penalty` (0.1, max 10 counted), floor 0.0. Findings list file/line/message. | diagnostics preserved even on failure |
| `static_analysis` | Built-in deterministic checks (`gets`→high, `strcpy`/`strcat`/`sprintf`→medium, `system`→medium, `scanf` without width→low) + cppcheck when installed (`tool` declared per finding) | 1.0 − Σ severity penalties, floor 0.0. Never fabricates cppcheck output. | `unsupported` if no checks apply |
| `modularity` | Function decomposition: count, LOC per function (regex parse + `parse_confidence`) | Penalties for functions over `max_loc_per_function` (40) and single-function programs; `main` excludable from length. | parse confidence recorded |
| `complexity` | Two legs: **static** (loop-nesting depth, recursion → O(1)/O(n)/O(n²)/worse) and **timing** (scaled inputs in sandbox) | Static inference vs `rules.complexity.required` (`mismatch_penalty` 0.5). **Timing is corroboration only — never presented as proof of Big-O** (recorded as a limitation in every report). | `inconclusive`/`unsupported` when neither leg applies |
| `memory_safety` | ASan+UBSan instrumented build, runs trusted tests, parses sanitizer output | Clean = 1.0; each finding −`finding_penalty` (0.25), floor 0.0. Note: ASan needs unlimited `RLIMIT_AS` (virtual); physical memory still capped. | `unsupported` if sanitizer build fails on host; `inconclusive` with no trusted tests to run |
| `readability` | LLM rubric review (`prompts/code_review.txt`): naming, formatting, comments, structure (each 0–1) | Deterministic mean of dimensions → **bounded quantization** (nearest 0.05). No validated structured response → no score. | `inconclusive` (no gateway / validation failed after retries) |
| `similarity` | Normalized-token shingle Jaccard vs corpus (`corpus_paths`, ≤200 files) | `1 − max_similarity`; flags above `threshold` for **human review**. The code, report text, and this doc never claim authorship proof or LLM-authorship detection — a unit test asserts the report language. | `skipped` (empty corpus) |

## Tool availability effects

- No cppcheck → static_analysis runs built-in checks only (stated).
- No sanitizer support → memory_safety `unsupported` (stated).
- No Ollama → readability `inconclusive` (stated); correctness `inconclusive`
  (no trusted tests).
- Empty similarity corpus → `skipped` (stated).

In all cases the report records *why* in `limitations`/`uncertainty` — a
missing tool is never a silent perfect score.
