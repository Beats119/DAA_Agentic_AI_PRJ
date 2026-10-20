# Repository Audit — AutoGrader (Phase A, pre-fix)

**Date:** 2026-10-20 · **Scope:** entire tree of `~/workspace/daa_prj`
(**189 files** excluding `.venv/` and `__pycache__/`) · **Mode:** read-only —
no production code or tests were changed by this audit.

**Environment (this VM):** runs as **root (uid 0)**, kernel 7.0.0-39-generic,
Landlock ABI reported by the code's probe = 1 (code drives ABI v1 only),
network namespaces creatable, user namespaces not usable, cgroup v2 not
delegated, gcc 13.3 present, **Ollama / cppcheck / valgrind / lizard / perf
absent**. Test baseline re-run during this audit: **203 passed, 4 skipped**
(the 4 skips are the real-Ollama tests in `tests/integration/test_ollama_real.py`).

**Method:** every file read; behavior verified by targeted live probes where
cheap (sandbox read-exfiltration, zero-evidence scoring, coordinator
semantics, YAML rules upload, demo-command shell parse, full FakeGateway
evaluation). Statuses: **PASS** (behavior verified correct/honest),
**PARTIAL** (works, with defects or undisclosed caveats), **FAIL** (behavior
wrong or unsafe), **UNVERIFIED** (could not be exercised here — reason given),
**NOT_APPLICABLE** (no behavior to verify, e.g. pure data/spec files).

**Status tally (148 matrix rows; trivial package markers, fixtures, and
demo-artifact payloads share grouped rows, so rows < files):**
PASS 69 · PARTIAL 45 · FAIL 7 · NOT_APPLICABLE 26 · UNVERIFIED 1 file row
(`test_ollama_real.py`) plus the 6 environment-blocked items listed near
the end of the matrix.

**Post-remediation status (2026-10-20, Phases B–N — see the Resolution
section at the end of this document):** all 28 registered defects
(D1–D28) are resolved: **28 FIXED** (two with environment caveats noted
in the register: D13's sandboxed cppcheck is unexercised on this VM
because cppcheck is not installed; D24/D25 were closed by the Phase N
documentation pass itself). All 7 FAIL matrix rows are eliminated
(FAIL 7 → 0): `sandbox/landlock.py`, `sandbox/executor.py`,
`testing/generation/budgets.py`, `analyzers/complexity/analyzer.py`,
`orchestration/coordinator.py`, `docs/DEMO.md`, `requirements.lock`.
The matrix below is preserved **unchanged** as the pre-fix record; the
Resolution section is the authoritative after-state. Post-fix suite:
**348 passed, 4 skipped, 0 failed** (the 4 skips remain the
real-Ollama tests — still UNVERIFIED here, still the gate for the WSL
machine).

---

## Verdicts on the 17 mandated checks

| # | Check | Verdict | Evidence |
|---|-------|---------|----------|
| 1 | Landlock grants read under `/` | **CONFIRMED → D1** | `sandbox/landlock.py:115-118` `default_rules()` returns `("/", FS_READ_EXEC)`. Live probe: a sandboxed program read `/etc/shadow` and `~/.ssh/id_ed25519` and exited 0. Only *writes* are contained. |
| 2 | netns best-effort, run continues | **CONFIRMED → D3** | `sandbox/executor.py:324-325` appends warning `"network namespace unavailable; program ran with host networking"` and continues; `network_isolated` is reported honestly but nothing refuses to run. `ensure_available()` gates only on rlimits + killpg. |
| 3 | Seccomp probe is allow-all; no restrictive filter | **CONFIRMED → D4** | `sandbox/capabilities.py` `_probe_seccomp` installs a `RET_ALLOW` filter; module docstring admits no restrictive filter is installed anywhere. `seccomp: True` means "mechanism exists", not "filtering active". |
| 4 | `preexec_fn` in threaded app; child uid | **CONFIRMED → D2** | `executor.py:284` uses `preexec_fn` (unshare/Landlock/rlimits inside) while `orchestration/coordinator.py` and the API run work on `ThreadPoolExecutor`s. **No setuid/setgid anywhere in the tree** (grep-verified). Live probe: sandbox child `id` = `uid=0(root)`; the test suite itself emits CPython's "multi-threaded fork() may deadlock" DeprecationWarning. `RLIMIT_NPROC` is not enforced for uid 0. |
| 5 | Coordinator timeout ineffective | **CONFIRMED → D5** | `orchestration/coordinator.py:73-77`: `as_completed(future_to_job)` is called **without** a timeout; `future.result(timeout=...)` is then called on futures `as_completed` has already yielded (i.e. finished), so it never fires. The `with ThreadPoolExecutor` exit does `shutdown(wait=True)` and blocks forever on a hung analyzer; threads cannot be killed. |
| 6 | Zero-evidence grade; original vs effective weight | **PARTIALLY CONFIRMED → D6, D7** | Default rules (a *required* metric missing) correctly yield grade `None`/`incomplete`. But live probe: **all metrics optional + `renormalize` (or `exclude`) policy → grade `0.0`, `completed_with_warnings`** — a manufactured zero from zero eligible evidence. The `zero` policy's 0.0 is explicit policy and is fine. Separately: `scoring/engine.py` `MetricScore` and `core/schemas.py` `ReportMetric` expose **only the effective weight** (single `weight` field); the configured weight survives only inside a free-text note when renormalized. Totals do reconcile (`reporting/schemas.py` tolerances 0.05 / 1e-6). |
| 7 | Uploads read whole file before size check | **CONFIRMED → D17** | `api/app.py`: all three upload handlers start with `data = await file.read()` and only then compare `len(data)` to the cap. A multi-GB upload is fully buffered in memory before rejection. |
| 8 | Similarity empty corpus manufactures 1.0 | **REFUTED** | `analyzers/similarity/analyzer.py`: empty/missing corpus → status `skipped`, score `None`, with an explicit limitation. Honest. |
| 9 | ASan `unlimited_address_space` | **PARTIALLY CONFIRMED → D12** | `analyzers/memory_safety/analyzer.py:158` sets it (ASan needs virtual space). Remaining limits: hard RSS cap 512 MB, wall/CPU time, NPROC/FSIZE/STACK rlimits, output cap, killpg, Landlock, netns. The trade-off is documented in the module docstring but the **result's limitations omit both the unlimited-AS fact and `detect_leaks=0`** (leaks are never detected). Edge: a genuine student sanitizer-*build* failure is mislabeled `unsupported`. |
| 10 | DEMO.md shell continuation | **CONFIRMED → D25** | The `--source ... \\   # and buggy_add.c` line: `\\` is an escaped backslash (a literal `\` argument), the comment then ends the command. Verbatim dry run (with `echo`): prints a stray `\` argument, then `--rules: command not found`, exit 127. |
| 11 | API error leakage / traversal | **MOSTLY REFUTED → D19 (minors)** | Solid: run ids validated by `^[0-9a-f]{32}$` on every run route; artifact names allowlisted (`report.json/html`, `evidence.json`, `manifest.json`, `source.c`); no traceback is ever returned. Minors: 422 responses embed loader error text containing **server file paths**; `_run_state` returns raw exception text; `meta.json` is rewritten non-atomically with no lock; `next(assignments_dir.glob(...))` has no default → 500 if the upload file vanished. |
| 12 | Feedback verification coverage | **PARTIALLY CONFIRMED → D21** | `orchestration/feedback.py` `verify_feedback` genuinely checks: cited evidence ids exist; claimed failures map to recorded **trusted** results and actually failed; grade-restatement regex; compile-failure contradiction (keyword, best-effort). **Not checked:** claims citing non-test evidence (waved through by design comment), recommendations, and any **source-location claims** (e.g. "line 42") — never validated against the source. |
| 13 | Caching could return old tests/grades | **REFUTED** (but see D10) | `testing/generation/agent.py` makes fresh LLM calls every run; tree-wide grep finds no test/grade cache — the only `lru_cache` is the Landlock ABI probe. However `token_budget`/`exec_budget` are computed by `testing/generation/budgets.py` and **never enforced anywhere** → D10. |
| 14 | Persistence immutability / SQLite threading | **MOSTLY REFUTED → D26 (minors)** | `persistence/store.py` enforces immutability (artifact writes refuse overwrites; terminal runs raise `ImmutableRunError`), writes files atomically (mkstemp + `os.replace`), `_safe_name` rejects `..`; single connection + `RLock` + `check_same_thread=False`. Minors: file is written **before** the DB insert (crash → orphan file); no WAL / `busy_timeout`; the workflow opens a **second** `RunStore` per evaluation while the API holds its own — the lock does not span instances, so concurrent runs risk `database is locked`. |
| 15 | Hardcoded models / API keys | **MOSTLY REFUTED → D27 (minors)** | No API-key concept anywhere (grep-verified); client is loopback-only and sends no `Authorization` header. Models come from `configs/models.yaml` via `model_cfg_for`. Minors: `ollama/models.py` `_DEFAULTS` hardcodes a `"llama3.1"` fallback in code; the gateway never sends `max_tokens` as `num_predict`, so configured response-length limits are unenforced. |
| 16 | Correctness counts only `trusted` tests | **REFUTED** (edge → D15) | Code path verified in `analyzers/correctness/analyzer.py`: filter is `status == "trusted"` only; zero trusted → `inconclusive`, score `None`; exploratory runs are opt-in and unscored. Edge: an executor `SandboxError` inside `_run_one` is recorded as `passed=False` — an infrastructure failure counts as a student test failure. |
| 17 | ACCEPTANCE.md overclaims | **CONFIRMED → D24** | Item 7 ("Untrusted C runs in verified isolation — PASS") is not supportable against D1–D4 (no read isolation, root children, best-effort netns, no seccomp). Item 19 leans on DEMO.md whose printed command cannot run (D25). Items 5 and 17 are honestly marked PASS-WITH-NOTE. `docs/SANDBOX.md` separately claims students "cannot read or write each other's files (Landlock)" — false for reads. |

---

## Audit matrix

### Core (`autograder/app/core/`)

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `core/numbers.py` | Decimal money/score math | PASS | Converts via `str()`, `ROUND_HALF_UP`; no float leakage found. | `tests/unit/test_scoring.py`, `test_comparison.py` | None |
| `core/comparison.py` | Output comparison policies (exact / normalize_trailing, case) | PASS | Policies applied consistently; trailing-whitespace normalization tested. | `tests/unit/test_comparison.py` | None |
| `core/schemas.py` | All Pydantic contracts (rules, spec, tests, report, manifest) | PARTIAL | Strong: `extra="forbid"`, weights-sum tolerance 1e-9 unless `normalize_weights`, `max_rounds ≤ 3`, finite/range grade validators. Defects: `ReportMetric` has only effective `weight` (→ D7); `Report.scoring_policy_version` default `"1.0"` duplicates the engine constant (drift risk); `FailingTest` provenance fallback elsewhere constructs `tier="unknown"` outside the `ExpectedProvenance` Literal set (latent raise inside an `except` handler in workflow report building). | `tests/unit/test_schemas.py`, `test_rules.py` | Phase C: add `original_weight`; single-source the policy version. Phase E: fix the provenance fallback literal. |
| `core/__init__.py` | Package marker | NOT_APPLICABLE | Docstring only. | — | None |

### Rules (`autograder/app/rules/`)

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `rules/validate.py` | Semantic rules validation beyond schema | PASS | Rejects unknown analyzers, bad thresholds; errors are field-scoped. | `tests/unit/test_rules.py` | None |
| `rules/loader.py` | Load JSON/YAML rules from disk | PASS | Dispatches on suffix; read errors name the path (that text leaks into API 422s → D19). | `tests/unit/test_rules.py` | Phase H: sanitize loader errors at the API boundary. |
| `rules/versioning.py` | Canonical-JSON sha256 rules hash | PASS | Canonicalization stable; hash recorded in report + manifest. | `tests/unit/test_rules.py` | None |
| `rules/__init__.py` | Package marker | NOT_APPLICABLE | — | — | None |

### Persistence (`autograder/app/persistence/`)

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `persistence/store.py` | SQLite run index + immutable artifact store | PARTIAL | Immutability genuinely enforced; atomic file writes; traversal-safe names. Minors: file-before-insert orphan window; no WAL/`busy_timeout`; per-method commits (→ D26). | `tests/unit/test_persistence.py` | Phase F: WAL + busy_timeout; insert-then-write or reconcile orphans. |
| `persistence/__init__.py` | Package marker | NOT_APPLICABLE | — | — | None |

### Ollama gateway (`autograder/app/ollama/`)

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `ollama/client.py` | Loopback-only structured generation client | PARTIAL | Rejects non-loopback URLs at construction; no auth header exists; validation retries bounded. Defect: never sends `max_tokens` as `num_predict` → response length unbounded by config (→ D27). | `tests/integration/test_ollama_gateway.py` | Phase D: pass `num_predict` from model config. |
| `ollama/health.py` | Reachability/model probe | PASS | Honest `reachable/models/error`; small-model hints are recommendation-only, not selection. | `tests/integration/test_ollama_gateway.py` | None |
| `ollama/models.py` | Task → model config from `configs/models.yaml` | PARTIAL | Config-driven as required; `_DEFAULTS` fallback hardcodes `"llama3.1"` in code (→ D27); `feedback_check` task defined but never invoked by any code (→ D22). | `tests/integration/test_ollama_gateway.py` | Phase D/K: drop or document the in-code fallback; remove or wire `feedback_check`. |
| `ollama/prompts.py` | Versioned prompt loading + hashing | PASS | Front-matter version parsed; prompt hash recorded in `GenerationRecord`. | `tests/integration/test_spec_extraction.py` | None |
| `ollama/__init__.py` | Package marker | NOT_APPLICABLE | — | — | None |

### Ingestion & specification

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `ingestion/pdf.py` | PDF/text assignment ingestion (PyMuPDF) | PASS | Page-structured extraction; text fallback honest. | `tests/unit/test_ingestion.py` | None |
| `ingestion/__init__.py` | Package marker | NOT_APPLICABLE | — | — | None |
| `specification/agent.py` | LLM spec extraction (explicit/inferred/ambiguous) | PARTIAL | Correctly demotes excerpt-less "explicit" requirements to `inferred`. Minor: 12 000-char input truncation is not recorded in diagnostics, so a truncated spec looks complete. | `tests/integration/test_spec_extraction.py` | Phase D: record truncation in diagnostics. |
| `specification/__init__.py` | Package marker | NOT_APPLICABLE | — | — | None |

### Testing — generation, validation, oracle (`autograder/app/testing/`)

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `testing/generation/agent.py` | Fresh LLM candidate generation + bounded refinement | PARTIAL | Genuinely fresh every call — no caching/memoization (check 13 refuted). Defect: budgets from `budgets.py` are accepted but never enforced here or anywhere (→ D10). | `tests/integration/test_generation_oracle.py` | Phase D: enforce candidate/token budgets in the loop. |
| `testing/generation/budgets.py` | Effective token/exec budget computation | FAIL | Computes capped budgets that **no caller consumes** — dead enforcement; a run's total test executions (correctness + memory_safety + complexity re-runs) are unbounded by any global cap (→ D10). | None assert enforcement | Phase D: wire budgets into generation + analyzers, or delete the module and the rules fields. |
| `testing/generation/__init__.py` | Exports | NOT_APPLICABLE | — | — | None |
| `testing/validation/cases.py` | Deterministic candidate validation | PARTIAL | Rejects malformed candidates; integer-detection heuristic is crude but disclosed in comments. | `tests/integration/test_generation_oracle.py` | None (disclosed heuristic) |
| `testing/validation/__init__.py` | Exports | NOT_APPLICABLE | — | — | None |
| `testing/oracle/strategies.py` | Tier 1/2/3 oracle strategies | PARTIAL | Tier-3 agreement is hard-capped at `provisional` (assertion + test). Defect: the `ctx.reference_executor` path of `ReferenceSolutionOracle.assess` returns `trusted` when the reference merely *runs*, **without comparing its output to the candidate's expected output** (→ D9). That path is never wired by production callers today — latent trap. | `tests/unit/test_schemas.py` (provenance), `test_generation_oracle.py`, `tests/security/test_oracle_sandbox.py` | Phase D: compare outputs in `assess`, or remove the executor path. |
| `testing/oracle/service.py` | Oracle assessment orchestration + quarantine | PARTIAL | Quarantine bounded (default 3 rounds), rejection reasons recorded. Defect: `ReferenceSolutionOracle.prepare()` marks the reference ready/**trusted on compilation alone when the spec has no examples** — "no spec examples to validate against" is noted but trust is not downgraded (→ D8). | `tests/integration/test_generation_oracle.py` | Phase D: no-example reference → cap at `provisional` or require explicit instructor opt-in. |
| `testing/oracle/__init__.py` | Exports | NOT_APPLICABLE | — | — | None |
| `testing/__init__.py` | Package marker | NOT_APPLICABLE | — | — | None |

### Sandbox (`autograder/app/sandbox/`)

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `sandbox/landlock.py` | Landlock policy + ABI probe | FAIL | `default_rules()` grants `("/", FS_READ_EXEC)` — **no read isolation** (→ D1, live-exploited). ABI probing is fork-free by design (Phase 6 EAGAIN fix is real). Only ABI v1 is driven. | `tests/security/test_fs_escape.py` (writes only — no read test exists) | Phase B: default-deny reads; allowlist system paths + run dir; add a read-exfiltration test. |
| `sandbox/executor.py` | Sandboxed process execution (rlimits, netns, Landlock, killpg, output caps) | FAIL | `preexec_fn` from a multithreaded app; **children run as the invoking uid (root here)** with no setuid/setgid (→ D2). netns failure = warning + host networking (→ D3). Landlock failure *is* fail-closed per run (child exits 125 pre-exec) and `ensure_available()` fail-closes on rlimits/killpg — those parts are genuinely good. | `tests/security/` (11 modules, real hostile binaries) | Phase B: dedicated unprivileged identity (or userns), `posix_spawn`/helper-process instead of `preexec_fn`, refuse when Landlock/netns unavailable. |
| `sandbox/capabilities.py` | Host capability probe | PARTIAL | Honest booleans for netns/userns/cgroup/rlimits/compiler. Misleading bits: `seccomp=True` reflects an allow-all probe filter, not filtering (→ D4); the fork-based userns probe triggers CPython's multithreaded-fork DeprecationWarning in-process. | `tests/security/test_capabilities.py` | Phase B: rename/report seccomp as "mechanism present, no filter installed"; probe userns out-of-process. |
| `sandbox/compiler.py` | Sandboxed C compilation | PASS | Compiles *inside* the sandbox, `TMPDIR` redirected into the run dir, `allowed_dir` traversal guard on the source path. | `tests/security/test_compile_fail.py` | None |
| `sandbox/__init__.py` | Exports | NOT_APPLICABLE | — | — | None |

### Analyzers (`autograder/app/analyzers/`)

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `analyzers/base.py` | Common analyzer contract + honesty guard | PASS | `build_result` raises if a non-`completed` status carries a score — manufactured scores are structurally blocked at this layer. | `tests/unit/test_analyzers_base.py` | None |
| `analyzers/correctness/analyzer.py` | Run trusted tests, score pass rate | PARTIAL | Trusted-only scoring verified in the code path (check 16 refuted); zero trusted → `inconclusive`/`None`. Edge: executor `SandboxError` in `_run_one` recorded as `passed=False` — infrastructure failure counted as student failure (→ D15). | `tests/unit/test_analyzers_correctness.py` | Phase G: distinguish infra errors from test failures (exclude + warn). |
| `analyzers/compiler/analyzer.py` | Warnings/errors → score | PARTIAL | Warnings degrade, errors → `failed`; honest. Minor: a compile *timeout* is conflated into "compilation failed". | `tests/unit/test_analyzers_compiler.py` | Phase G: report timeout distinctly. |
| `analyzers/static_analysis/analyzer.py` | Built-in checks + cppcheck | PARTIAL | Built-in checks real; cppcheck absence disclosed in limitations. Defect: cppcheck is run via `subprocess.run` **on the host, unsandboxed**, parsing untrusted source (→ D13). | `tests/unit/test_analyzers_static.py` | Phase B/G: run cppcheck inside the sandbox executor. |
| `analyzers/static_analysis/checks.py` | Built-in pattern checks (dangerous calls, scanf width, main return) | PASS | Pure function, honestly scoped ("deliberately simple… NOT a replacement for cppcheck"). Nits within declared scope: `/* */` block comments aren't stripped (possible false positive inside a block comment); the main-no-return check accepts a `return` in any later function. | `tests/unit/test_analyzers_static.py` | None (optional: strip block comments in Phase G) |
| `analyzers/modularity/analyzer.py` | Function/structure metrics | PASS | Regex parsing limits disclosed (`parse_confidence` + limitations in every result). | `tests/unit/test_analyzers_modularity.py` | None |
| `analyzers/complexity/analyzer.py` | Static Big-O inference + timing leg | FAIL | When rules omit `complexity.required`, `score_when_no_requirement` awards **1.0 at full metric weight** — a manufactured full score from an unchecked heuristic (→ D11). Timing leg correctly never scores (§13.6 caveat present). | `tests/unit/test_analyzers_complexity.py` (does not cover the no-requirement default) | Phase G: no requirement → `inconclusive`/`None` (or explicit opt-in default in rules). |
| `analyzers/memory_safety/analyzer.py` | ASan/UBSan runs over trusted tests | PARTIAL | Real instrumented builds; no trusted tests → `inconclusive`. Defects: result limitations omit unlimited-AS + `detect_leaks=0` (→ D12); student-caused sanitizer build failure mislabeled `unsupported`. | `tests/unit/test_analyzers_memory.py` | Phase G: disclose both facts in limitations; distinguish build-failure causes. |
| `analyzers/readability/analyzer.py` | LLM rubric code review | PARTIAL | No gateway → `inconclusive`; scores quantized to 0.05. Defect: `CodeReview.criteria` has no `min_length` — an **empty review validates and scores 0.0 as `completed`** (manufactured zero); missing dimensions are silently averaged over present ones (→ D14). | `tests/unit/test_analyzers_readability.py` | Phase G: require all rubric dimensions; empty review → `inconclusive`. |
| `analyzers/similarity/analyzer.py` | Shingle-Jaccard vs corpus, human-review flag | PASS | Empty corpus → `skipped`/`None` (check 8 refuted); language never claims authorship — asserted by test. | `tests/unit/test_analyzers_similarity.py` | None |
| `analyzers/__init__.py` + 8 subpackage `__init__.py` | Package markers | NOT_APPLICABLE | Docstrings only. | — | None |

### Orchestration (`autograder/app/orchestration/`)

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `orchestration/workflow.py` | End-to-end evaluation pipeline | PARTIAL | Much is right: fail-closed on `SandboxUnavailable`; artifacts persisted *before* the terminal state transition; honest degraded no-gateway path; analyzer warnings merged into `report.uncertainty`; fresh generation every run. Defects: plan built with `spec=None` **before** extraction, so the false limitation *"no specification available: LLM test generation is impossible…"* is merged into **every** run — proven live on a successful FakeGateway run (grade 58.9) (→ D16); `_ref_compile` passes `allowed_dir=None`, dropping the traversal guard for the rules-supplied reference path; `_finish_aborted` silently swallows artifact-write errors; `s.prepare(compile=<dummy lambda>)` is dead/misleading (ignored when a sandbox executor is set). | `tests/integration/test_orchestration.py` | Phase E: build/refresh the plan after spec extraction (or scope the warning); pass an `allowed_dir`; log aborted-run artifact failures. |
| `orchestration/coordinator.py` | Bounded parallel analyzer execution | FAIL | Per-analyzer timeout is **ineffective** (→ D5): `as_completed` without timeout + `result(timeout=)` on finished futures + `shutdown(wait=True)` — one hung analyzer hangs the whole evaluation forever. Module docstring claims the opposite. | `tests/integration/test_orchestration.py` (no hanging-analyzer test exists) | Phase E: real deadline handling (poll loop / `as_completed(timeout=)`), non-blocking shutdown, hanging-analyzer test. |
| `orchestration/planner.py` | Deterministic plan from (spec, rules) | PARTIAL | Analyzer availability probing in one place; honest warnings. Minor: `PARALLEL_ANALYZERS` constant is unused and its comment ("correctness excluded") doesn't match the workflow, which runs all analyzers through the coordinator. | `tests/integration/test_orchestration.py` | Phase E: remove/reconcile the dead constant. |
| `orchestration/evidence.py` | Append-only evidence registry | PARTIAL | Append-only enforced; `finalize()` locks; ids deterministic. Minor: `append_blob` swallows store exceptions — a record can cite a blob hash whose bytes were never persisted (→ D28). | `tests/unit/test_persistence.py`, `test_orchestration.py` | Phase F: propagate or mark blob-persistence failures. |
| `orchestration/feedback.py` | Feedback generation + deterministic verification | PARTIAL | Verification is real (evidence ids, trusted-result cross-check, grade-restatement, bounded retry, zero-LLM fallback; feedback never touches the grade). Gaps → D21: non-test-evidence claims waved through; recommendations and source-location claims unchecked. | `tests/integration/test_orchestration.py` | Phase F: check location claims against source or strip them; verify failure claims for all evidence kinds. |
| `orchestration/run.py` | CLI runner + gateway wiring | PASS | Degraded path explicit; failures become `failed` run records without tracebacks/paths in reports. | `tests/integration/test_orchestration.py` | None |
| `orchestration/__init__.py` | Package marker | NOT_APPLICABLE | — | — | None |

### Scoring & reporting

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `scoring/engine.py` | Deterministic weighted scoring (policy 1.0) | PARTIAL | Pure function of evidence; Decimal math; duplicates raise; unknown metrics warned. Defects: zero-eligible-evidence → grade `0.0` under `renormalize`/`exclude` when all metrics are optional (→ D6); only effective weight exposed (→ D7). **A unit test pins the defective 0.0 behavior** and must be updated with the fix. | `tests/unit/test_scoring.py` | Phase C: no eligible evidence → grade `None` + `incomplete`/`completed_with_warnings` per required-missing policy; expose original + effective weights. |
| `scoring/__init__.py` | Package marker | NOT_APPLICABLE | — | — | None |
| `reporting/schemas.py` | Report validation/reconciliation | PASS | Reconciles Σ contributions vs grade (0.05) and per-metric `100·w·s` (1e-6); reconciliation only attempted when a grade exists — consistent with D6 being an engine issue, not a reporting one. | `tests/unit/test_report.py` | None |
| `reporting/html.py` | HTML report fragment rendering | PASS | Autoescape on; belt-and-braces absolute-path redaction filter; renders exactly the validated report's numbers. | `tests/unit/test_report.py`, `tests/integration/test_api.py` | None |
| `reporting/__init__.py` | Package marker | NOT_APPLICABLE | — | — | None |

### API, templates, static (`autograder/app/api/`, `templates/`, `static/`)

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `api/app.py` | FastAPI REST + pages | PARTIAL | Strong traversal hygiene (run-id regex, artifact allowlist). Defects: whole-upload buffering before size checks (→ D17); **YAML rules upload broken** — stored renamed to `{uuid}.json`, returns 201 + hash, then never lists or resolves (proven live) (→ D18); minor path text in 422s, unlocked non-atomic `meta.json`, `next(glob())` 500 edge (→ D19). | `tests/integration/test_api.py` (YAML-upload path untested) | Phase H: streaming size-capped reads; preserve rules file extension; lock/atomic meta writes; sanitize error text. |
| `api/main.py` | Server entrypoint (.env, uvicorn) | PASS | Loopback default host; gateway factory probes before constructing a client. | `tests/integration/test_api.py` | None |
| `api/__init__.py` | Package marker | NOT_APPLICABLE | — | — | None |
| `templates/base.html` | Layout + CSS | PASS | No unescaped user content; inline CSS only. | — (rendered in API tests) | None |
| `templates/index.html` | Dashboard | PASS | Grade shown or `—` when `None` — no fake zeros. | `test_api.py` | None |
| `templates/new_evaluation.html` | Evaluation form | PASS | Uses API via `app.js`; selections only. | `test_api.py` | None |
| `templates/run_detail.html` | Run status + report embed | PASS | `{{ report_html|safe }}` is correct composition: the fragment is itself autoescaped at render time. | `test_api.py` | None |
| `templates/report_page.html` | Standalone report page | PASS | Same `|safe` composition as above. | `test_api.py` | None |
| `templates/report_fragment.html` | Grade/metrics/feedback fragment | PARTIAL | All dynamic fields escaped; weights column shows the (effective) `weight` with no original-weight counterpart (→ D7, display side); similarity section carries the required "not proof of authorship" language. | `test_report.py`, `test_api.py` | Phase C: show original + effective weight when renormalized. |
| `templates/rules.html` | Rules registry UI | PASS | Dynamic inserts go through `AG.esc()`. | `test_api.py` | None |
| `templates/operations.html` | Ollama/sandbox/version status | PARTIAL | Renders probe output honestly, but surfaces the misleading `seccomp: True` without the "no filter installed" caveat (→ D4 display side). | `test_api.py` | Phase B: render the corrected capability semantics. |
| `static/app.js` | Polling + upload flow | PASS | `esc()` applied to all dynamic strings; bounded polling (240 × 2 s) with an honest timeout message. | — | None |

### Configs, prompts, examples

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `configs/models.yaml` | Task → model/params | PASS | All six tasks configured (temperature 0, seeds set); source of truth for models. `feedback_check` entry is unused by code (→ D22). | `test_ollama_gateway.py` | Phase K: remove or wire `feedback_check`. |
| `prompts/specification.txt` | Spec-extraction prompt v1.0 | PASS | Versioned front matter; hash recorded per run. | `test_spec_extraction.py` | None |
| `prompts/test_generation.txt` | Candidate-generation prompt v1.0 | PASS | Versioned; instructs fresh candidates. | `test_generation_oracle.py` | None |
| `prompts/test_refinement.txt` | Refinement prompt v1.0 | PASS | Versioned. | `test_generation_oracle.py` | None |
| `prompts/oracle_verify.txt` | Second-path verification prompt | PASS | Versioned; its outputs are capped at `provisional` by code, as the prompt's role requires. | `test_generation_oracle.py` | None |
| `prompts/code_review.txt` | Readability rubric prompt | PASS | Versioned; rubric dimensions match the analyzer's expectations (the empty-criteria hole is in the analyzer's schema, → D14). | `test_analyzers_readability.py` | None |
| `prompts/feedback.txt` | Feedback drafting prompt | PASS | Versioned; instructs evidence-only claims (verification is the enforcement, → D21). | `test_orchestration.py` | None |
| `prompts/feedback_check.txt` | LLM feedback-check prompt | PARTIAL | **Dead asset** — no code loads it; the supplementary LLM check it implies does not exist (→ D22). | None | Phase K: implement the pass or delete the prompt + config entries. |
| `examples/rules/add.json` | Annotated example rules | PASS | All sections demonstrated; weights sum to 1.0; no embedded expected answers. Note: `compile.flags` repeats `-std=c11` alongside `std: "c11"` (harmless duplication). | `test_rules.py` | None |

### Stub / empty packages

| Path | Responsibility | Status | Findings | Covering tests | Required change |
|---|---|---|---|---|---|
| `autograder/__init__.py`, `autograder/app/__init__.py` | Package markers | NOT_APPLICABLE | Accurate docstrings. | — | None |
| `autograder/app/evidence/__init__.py` | Claims "evidence registry" | PARTIAL | **Empty stub** — the registry actually lives in `orchestration/evidence.py`; README's layout section repeats the claim (→ D22). | — | Phase K: delete the stub or move the registry here. |
| `autograder/app/agents/__init__.py` | Claims "LLM agents" | PARTIAL | **Empty stub** — agents live in `specification/` and `testing/` (→ D22). | — | Phase K: delete the stub. |
| `schemas/`, `scripts/` (root, empty dirs) | — | NOT_APPLICABLE | Leftover empty directories, no files. | — | Phase K: remove. |
| `autograder.egg-info/*`, `.pytest_cache/*`, `artifacts/`, `reports/` | Generated build/test/run state | NOT_APPLICABLE | Not source: egg-info from the editable install, pytest cache, and local run outputs (all gitignored). The demo's committed `docs/demo/artifacts/autograder.db` is demo data, covered by the demo-artifacts row. | — | None |

### Tests (`tests/`)

Baseline re-verified in this audit: **203 passed, 4 skipped** (41.6 s, this VM).

| Path | Responsibility | Status | Findings | Required change |
|---|---|---|---|---|
| `tests/unit/conftest.py` | Unit fixtures (fake gateway, sample rules) | PASS | Mocks clearly labeled, test-only. | None |
| `tests/unit/test_schemas.py` | Schema contracts | PASS | Covers validators incl. weights-sum and round bounds. | None |
| `tests/unit/test_rules.py` | Rules load/validate/hash | PASS | Includes rejection cases. | None |
| `tests/unit/test_scoring.py` | Scoring math + policies | PARTIAL | Thorough — but one test **pins the defective zero-evidence behavior** ("nothing eligible: grade over empty set -> 0.0 with warnings") (→ D6). No test covers original-vs-effective weight display (→ D7). | Phase C: update the pinned expectation; add weight-provenance tests. |
| `tests/unit/test_comparison.py` | Comparison policies | PASS | — | None |
| `tests/unit/test_ingestion.py` | PDF/text ingestion | PASS | — | None |
| `tests/unit/test_persistence.py` | Store immutability/state machine | PASS | Immutability genuinely asserted. Does not test two `RunStore` instances on one DB (→ D26). | Phase F: add cross-instance concurrency test. |
| `tests/unit/test_report.py` | Report reconciliation + HTML | PASS | Reconciliation tolerances asserted; XSS escaping covered via API tests. | None |
| `tests/unit/test_analyzers_base.py` | Honesty guard | PASS | Asserts non-completed statuses can't carry scores. | None |
| `tests/unit/test_analyzers_correctness.py` | Trusted-only scoring | PASS | Asserts provisional/exploratory never score. Does not cover the SandboxError-as-failure edge (→ D15). | Phase G: add infra-failure test. |
| `tests/unit/test_analyzers_compiler.py` | Compiler analyzer | PASS | — | None |
| `tests/unit/test_analyzers_complexity.py` | Complexity analyzer | PARTIAL | Does not cover the no-`required` default that yields 1.0 (→ D11). | Phase G: add no-requirement test expecting `inconclusive`. |
| `tests/unit/test_analyzers_memory.py` | Memory-safety analyzer | PARTIAL | Does not assert the limitations disclose unlimited-AS / `detect_leaks=0` (→ D12). | Phase G: assert limitation text. |
| `tests/unit/test_analyzers_modularity.py` | Modularity analyzer | PASS | — | None |
| `tests/unit/test_analyzers_readability.py` | Readability analyzer | PARTIAL | Does not cover empty-`criteria` reviews (→ D14). | Phase G: add empty-review test. |
| `tests/unit/test_analyzers_similarity.py` | Similarity analyzer | PASS | Asserts empty-corpus honesty + no authorship language. | None |
| `tests/unit/test_analyzers_static.py` | Static analysis | PASS | cppcheck-absent path asserted. Host-execution of cppcheck untested (→ D13). | Phase G: test sandboxed cppcheck once wired. |
| `tests/unit/__init__.py` | Marker | NOT_APPLICABLE | — | None |
| `tests/integration/conftest.py` | Scripted mock Ollama server | PASS | Clearly labeled test-only; includes hang script used by timeout tests. | None |
| `tests/integration/test_ollama_gateway.py` | Gateway vs mock server | PASS | Retries, timeouts, loopback enforcement, no-auth-header all asserted. | None |
| `tests/integration/test_ollama_real.py` | Real-Ollama gate | UNVERIFIED | Skips explicitly here (no Ollama on this VM) — by design; must run on the target WSL machine. | Run on Khushi's WSL (outstanding since Phase 8). |
| `tests/integration/test_spec_extraction.py` | Spec agent vs mock | PASS | Explicit/inferred demotion covered. | None |
| `tests/integration/test_generation_oracle.py` | Generation + tiered oracle | PASS | Tier-3-never-trusted invariant asserted. No-example reference trust (→ D8) not covered. | Phase D: add no-examples reference test. |
| `tests/integration/test_orchestration.py` | Full workflow e2e (FakeGateway) | PARTIAL | Strong e2e (grade stability across 5 runs, degraded path, feedback-cannot-touch-grade). Gaps: no hanging-analyzer test (→ D5); limitations content not asserted (the false spec limitation, → D16, passes unnoticed); test-generation model-record provenance not asserted (→ D20). | Phase E: add hang + limitations assertions. |
| `tests/integration/test_api.py` | FastAPI endpoints | PARTIAL | Upload caps, traversal, XSS, 202/poll flow covered. YAML rules upload untested (→ D18); oversized-upload memory behavior untestable at unit scale (→ D17). | Phase H: add YAML upload round-trip test. |
| `tests/integration/__init__.py` | Marker | NOT_APPLICABLE | — | None |
| `tests/security/conftest.py` | Hostile-binary fixtures | PASS | Real compilation of hostile sources; no mocks. | None |
| `tests/security/test_capabilities.py` | Probe honesty | PASS | Type/repeatability assertions only — cannot catch the seccomp semantics issue (→ D4). | Phase B: assert "no filter installed" reporting. |
| `tests/security/test_compile_fail.py` | Compile diagnostics + traversal | PASS | — | None |
| `tests/security/test_crash.py` | Segfault recording | PASS | — | None |
| `tests/security/test_fail_closed.py` | Degraded-host refusal | PARTIAL | Covers only the two gates that exist (rlimits, killpg). No test refuses on missing Landlock-read-policy or missing netns because the code doesn't (→ D1/D3). | Phase B: extend once fail-closed is real. |
| `tests/security/test_forkbomb.py` | Fork-bomb containment | PARTIAL | Passes here, but as root `RLIMIT_NPROC` is unenforced — containment actually comes from wall-timeout + killpg; the test does not isolate which mechanism saved it. | Phase B: assert mechanism attribution in enforcement report. |
| `tests/security/test_fs_escape.py` | Landlock write containment | PARTIAL | Write escapes (absolute + `../../`) genuinely blocked. **No read test exists** — the D1 exfiltration is invisible to the suite. | Phase B: add read-exfiltration test (`/etc/shadow`, a canary file outside the run dir). |
| `tests/security/test_memory.py` | Memory bomb | PASS | `RLIMIT_AS` → SIGSEGV recorded honestly. | None |
| `tests/security/test_no_network_claim.py` | Network honesty | PASS | Correctly conditional: passes with or without isolation, asserts only that claims match reality. By design it cannot require isolation (→ D3 is a policy gap, not a test bug). | Phase B: add a strict-mode test once refusal exists. |
| `tests/security/test_oracle_sandbox.py` | Oracle under sandbox | PASS | Reference crash/timeout → `inconclusive`, never trusted. | None |
| `tests/security/test_output_flood.py` | Output caps | PASS | — | None |
| `tests/security/test_timeout.py` | Timeout + tree kill | PASS | Forked sleeper verified dead via `/proc` scan. | None |
| `tests/security/__init__.py` | Marker | NOT_APPLICABLE | — | None |
| `tests/fixtures/c/*.c` (6 files: correct/buggy/warning/unsafe/loop-heavy/overflow) | Sample submissions | NOT_APPLICABLE | Synthetic, labeled; production code does not branch on them. | None |
| `tests/fixtures/corpus/` (sample corpus files) | Similarity corpus | NOT_APPLICABLE | Synthetic data. | None |

### Docs (`docs/`, `README.md`)

| Path | Responsibility | Status | Findings | Required change |
|---|---|---|---|---|
| `docs/PLAN.md` | Governing spec | PASS | The reconciled master plan; audit measured the code against it (§13.6, §6 acceptance). | None |
| `docs/SANDBOX.md` | Sandbox boundary doc | PARTIAL | Mostly honest (discloses read+execute on `/`, best-effort netns, no seccomp). But: claims students "cannot read or write each other's files (Landlock)" — **false for reads** (→ D1/D24); fork-bomb credited to `RLIMIT_NPROC` which doesn't bind root; capability snapshot is stale (describes a uid-65534 host; this VM is uid 0). | Phase J: correct the read claim + mechanism attributions after Phase B. |
| `docs/ACCEPTANCE.md` | 22-point acceptance checklist | PARTIAL | 20 PASS + 2 PASS-WITH-NOTE as written, and items 5/17 are honest. Item 7 "verified isolation" PASS is an **overclaim** vs D1–D4; item 19 rests on the broken DEMO command (→ D24/D25). | Phase J: re-walk after fixes B–I; downgrade item 7 until read isolation + identity land. |
| `docs/DEMO.md` | Documented real demo runs | FAIL | The printed command **cannot run verbatim** (→ D25, proven: stray `\` arg + `--rules: command not found`, exit 127). The *results* section is honest (incomplete/None in degraded mode) and the saved reports are real. | Phase J: fix the command block; re-run verbatim to confirm. |
| `docs/demo/report_correct.json`, `report_buggy.json` | Real demo artifacts | PASS | Genuine outputs; statuses/grades match DEMO.md's table. They also evidence D20: manifests frozen at `state: "reporting"`, `tool_versions.python: "3.11+"`, `compiler: "gcc c11"` (not probed values). | Regenerate after Phase E fixes. |
| `docs/demo/artifacts/**` (run dirs, manifests, evidence) | Demo run artifacts | NOT_APPLICABLE | Raw outputs of the two demo runs; consistent with the reports. | None |
| `docs/HANDOFF.md` | Final handoff summary | PARTIAL | **Stale**: says Phases 6–8 are "pending push approval" — they were pushed 2026-10-20. "Fail-closed when isolation is unavailable" overstates (netns/read gaps, → D24). | Phase J/K: refresh status + safety wording. |
| `docs/ARCHITECTURE.md` | Architecture overview | PARTIAL | Claims the coordinator provides "per-analyzer timeouts" — ineffective in code (→ D5/D24). | Phase J: correct after Phase E. |
| `docs/README` n/a — `README.md` | Project front door | PARTIAL | Quickstart CLI block is valid shell (unlike DEMO.md). Repeats the dead `.env` keys (`DB_PATH`, `SANDBOX_POLICY`) and the `evidence/` layout claim (→ D22). "203 passed, 4 skipped" verified true. | Phase K: align with D22 cleanups. |
| `docs/INSTALL.md` | Install guide | PARTIAL | "The sandbox refuses to execute instead of running uncontained" — true only for rlimits/killpg/Landlock-application; netns absence and read exposure are not refusal conditions (→ D24). | Phase J: qualify after Phase B. |
| `docs/OLLAMA.md` | Ollama setup | PASS | Matches the loopback-only implementation; no API keys anywhere. | None |
| `docs/RULES.md` | Rules reference | PASS | Accurately documents sections incl. budgets — note it documents `token_budget`/`exec_budget` as if effective (→ D10 doc side). | Phase D/J: annotate budgets once enforced. |
| `docs/API.md` | API reference | PASS | Endpoints match `api/app.py`; upload caps stated without the buffering caveat (→ D17 doc side, minor). | Phase H: note streaming caps after fix. |
| `docs/ANALYZERS.md` | Analyzer reference | PARTIAL | Complexity row omits the no-`required` → 1.0 default (→ D11 doc side); memory-safety row mentions unlimited AS honestly. | Phase G/J: disclose/fix with D11/D12. |
| `docs/TESTING.md` | Test guide | PASS | Suite counts and skip semantics verified accurate in this audit. | None |
| `docs/TROUBLESHOOTING.md` | Troubleshooting | PASS | No claims contradicted by the code. | None |
| `docs/LIMITATIONS.md` | Known limitations | PARTIAL | Four concrete scenarios, all honest. Does **not** list: no read isolation in the sandbox, root-uid children, zero-evidence 0.0 under renormalize-with-all-optional, complexity 1.0 default. | Phase J: add the audit's P0/P1 limitations. |
| `docs/USE_CASES.md` | Use cases | PASS | Aspirational but consistent with implemented behavior; similarity use case correctly framed as triage. | None |
| `docs/TIMELINE.md` | Phase log | PASS | Counts per phase (117/136/186/194/203) match the build history. | None |
| `docs/VARIANCE.md` | Variance measurement | PASS | Honestly labels the FakeGateway measurement (0.00) and defers real-LLM variance to the Ollama machine. | None |
| `docs/demo/assignment_add.txt` | Demo assignment | NOT_APPLICABLE | Synthetic spec with a worked example. | None |

### Packaging

| Path | Responsibility | Status | Findings | Required change |
|---|---|---|---|---|
| `pyproject.toml` | Build/project metadata | PARTIAL | `pytest` is a **runtime** dependency (belongs in dev extras); templates/static are not declared as package data, so a non-editable wheel install would miss them (source/editable installs work). | Phase K: move pytest to dev; add package-data for templates/static. |
| `requirements.lock` | Pinned environment | FAIL | Contains `-e /home/hatch/workspace/daa_prj` — an editable line with **this VM's absolute path** (breaks `pip install -r` anywhere else) plus unrelated environment packages (onnxruntime, networkx, opentelemetry, pymupdf4llm…) that are not project dependencies. | Phase K: regenerate a clean, portable lock from `pyproject.toml`. |
| `.env.example` | Example configuration | PARTIAL | Truthfully states there are no API keys. But `SANDBOX_POLICY` and `DB_PATH` are **read by no code** (grep-verified) — a fictitious "permissive" sandbox mode is especially misleading (→ D22). | Phase K: delete dead keys or implement them. |
| `.gitignore` | VCS hygiene | PASS | Covers artifacts, DBs, `.env`, venvs. | None |

### Environment-blocked (UNVERIFIED) items

| Item | Why unverified |
|---|---|
| Real-LLM behavior: generation quality, true ±7 variance, real readability reviews | No Ollama on this VM. All LLM-path verdicts rest on the labeled FakeGateway/mock server. The 4 tests in `tests/integration/test_ollama_real.py` remain the gate — run on Khushi's WSL. |
| cppcheck/valgrind/lizard/perf paths | Tools absent here; code degrades honestly (`unsupported`/limitation), but the tools' real output parsing is unexercised. |
| Landlock ABI > v1 behavior | The code drives ABI v1 only; this kernel reports more, untested by design. |
| WSL2-specific behavior (target machine) | Different kernel/config; `docs/SANDBOX.md` expectations are plausible but unmeasured there. |
| Multi-run concurrency against one SQLite DB | Code inspection only (→ D26); no load test exists. |
| `docs/demo/artifacts/**` binary-adjacent payloads | Treated as data outputs; spot-checked via the two JSON reports only. |

---

## Defect register (pre-fix)

Severity: **P0** = safety/integrity boundary broken · **P1** = wrong results or
false claims in normal operation · **P2** = real defect, bounded blast radius.

| ID | Sev | Defect | Root cause | Fix phase |
|----|-----|--------|-----------|-----------|
| D1 | P0 | **No sandbox read isolation.** Sandboxed code reads the whole filesystem (`/etc/shadow`, SSH keys, other runs' files) — live-proven. | `sandbox/landlock.py` `default_rules()` grants `("/", FS_READ_EXEC)`; policy designed for write containment only. | B |
| D2 | P0 | **Sandbox children run as the invoking uid (root on this VM); `preexec_fn` used from a multithreaded app.** No setuid/setgid anywhere; `RLIMIT_NPROC` unenforced for uid 0; fork-in-thread deadlock risk (CPython warning observed). | Executor never drops privileges (userns path abandoned in Phase 4) and applies limits via `preexec_fn` while the coordinator/API run thread pools. | B |
| D3 | P0 | **Network namespace is best-effort** — failure only adds a warning; the program runs with host networking. Nothing downstream refuses. | `executor._execute` treats netns as optional; `ensure_available()` doesn't gate on it, contradicting the fail-closed spec. | B |
| D4 | P1 | **No restrictive seccomp filter exists**; the probe's allow-all filter makes `seccomp: True` misleading in `/health` and the operations page. | Probe tests mechanism presence only; no filter was ever designed/installed. | B |
| D5 | P1 | **Coordinator per-analyzer timeout is dead code** — a hung analyzer hangs the evaluation (and API worker) forever. | `as_completed()` called without timeout; `result(timeout=)` on already-completed futures; `shutdown(wait=True)` on executor exit. | E |
| D6 | P1 | **Manufactured grade 0.0 from zero eligible evidence** when all metrics are optional and policy is `renormalize`/`exclude` (live-proven). A unit test pins this behavior. | Engine aggregates over an empty eligible set and quantizes `100 × 0` instead of returning `None`. | C |
| D7 | P2 | **Configured (original) metric weight is nowhere exposed** — engine + report + HTML show only the effective weight; renormalization is visible only via a note string. | `MetricScore`/`ReportMetric` have a single `weight` field, deliberately storing the effective value for reconciliation. | C |
| D8 | P1 | **Reference oracle becomes trusted on compilation alone when the spec has no examples.** | `ReferenceSolutionOracle.prepare()` treats "no examples to validate against" as ready without downgrading trust. | D |
| D9 | P2 | **`ReferenceSolutionOracle.assess` via `ctx.reference_executor` returns `trusted` without comparing outputs.** Latent — no production caller wires it. | The non-sandbox assess path checks only that the reference ran. | D |
| D10 | P2 | **`token_budget`/`exec_budget` computed but never enforced**; trusted tests re-executed by up to 3 analyzers with no global execution cap. | `testing/generation/budgets.py` output has no consumers. | D |
| D11 | P1 | **Complexity awards 1.0 by default when rules omit `complexity.required`** — full-weight manufactured score from an unchecked heuristic; undisclosed in `docs/ANALYZERS.md`. | `score_when_no_requirement = 1.0` default in the analyzer. | G |
| D12 | P2 | **Memory-safety limitations omit the unlimited address space and `detect_leaks=0`** (leaks never detected); student sanitizer-build failures mislabeled `unsupported`. | Result limitations not populated with the executor trade-offs documented in the module docstring. | G |
| D13 | P2 | **cppcheck executes on the host, unsandboxed**, parsing untrusted source (30 s timeout only). | `static_analysis` analyzer calls `subprocess.run` directly instead of the sandbox executor. | B/G |
| D14 | P2 | **Readability: empty LLM review scores a `completed` 0.0**; missing rubric dimensions silently averaged over. | `CodeReview.criteria` lacks `min_length`; aggregate is a mean over whatever arrived. | G |
| D15 | P2 | **Correctness counts sandbox infrastructure failures as student test failures** (`SandboxError` → `passed=False`). | `_run_one` collapses executor exceptions into a failed result instead of an inconclusive/excluded outcome. | G |
| D16 | P1 | **Every report carries a false "no specification available" limitation**, including fully successful LLM runs (live-proven with FakeGateway). | Workflow builds the plan with `spec=None` before extraction and merges the plan's degraded-spec warning unconditionally. | E |
| D17 | P2 | **Uploads fully buffered in memory before size checks** (all three handlers) — memory-exhaustion surface. | `await file.read()` precedes every length comparison in `api/app.py`. | H |
| D18 | P2 | **YAML rules upload silently broken**: 201 + hash returned, file stored as `{uuid}.json`, never listed/usable (live-proven). | Upload handler renames the temp file to a `.json` name regardless of source format; listing dispatches on suffix. | H |
| D19 | P2 | **API minors**: 422 bodies embed server file paths (loader error text); `_run_state` returns raw exception text; `meta.json` rewritten non-atomically without a lock; `next(glob(...))` without default → 500 edge. | Error text passed through unsanitized; meta store predates concurrent-upload handling. | H |
| D20 | P2 | **Provenance inaccuracies**: manifest `state` frozen at `"reporting"`; `tool_versions` partly hardcoded (`"python": "3.11+"`, `"gcc c11"`, `"sandbox-1.0"` — not probed values); test-generation model records degrade to `"unknown"` (record-shape mismatch in workflow `_model_record`); `_finish_aborted` swallows artifact-write errors. | Manifest written once mid-pipeline and never updated; version strings hand-written; generation records lack the `"generation"` wrapper the reader expects. | E |
| D21 | P2 | **Feedback verification gaps**: failure claims citing non-test evidence pass unchecked; recommendations and source-location claims ("line 42") never validated. | `verify_feedback` only cross-checks test-result evidence; other claim types have no checker. | F |
| D22 | P2 | **Dead/misleading assets**: `.env.example` keys `SANDBOX_POLICY` (fictitious permissive mode) and `DB_PATH` read by nothing; `feedback_check` prompt + model task never invoked; empty stub packages `app/evidence/`, `app/agents/` with docstrings claiming responsibilities that live elsewhere; empty root dirs `schemas/`, `scripts/`. | Leftovers from earlier phase layouts, never reconciled. | K |
| D23 | P2 | **`requirements.lock` is not portable** (`-e /home/hatch/...` editable line + unrelated environment packages); `pytest` is a runtime dependency; templates/static not package data. | Lock generated from the dev VM's environment instead of the project's dependency set. | K |
| D24 | P1 | **Documentation overclaims**: `ACCEPTANCE.md` #7 "verified isolation" PASS; `SANDBOX.md` "cannot read … each other's files"; `ARCHITECTURE.md` "per-analyzer timeouts"; `INSTALL.md` unqualified refusal claim; `HANDOFF.md` stale push status + fail-closed overstatement; `LIMITATIONS.md` omits the P0/P1 items above. | Docs written against intended (Phase-4-era) behavior and not re-walked after the gaps were known. | J |
| D25 | P2 | **DEMO.md's printed command cannot run verbatim** (`\\` + trailing comment breaks continuation; proven exit 127). | Hand-edited code block never re-executed after editing. | J |
| D26 | P2 | **Persistence minors**: artifact file written before DB insert (orphan window); no WAL/`busy_timeout`; workflow opens a second `RunStore` per evaluation while the API holds its own (lock doesn't span instances → possible `database is locked`). | Single-instance assumption baked into the store design. | F |
| D27 | P2 | **Gateway config gaps**: `max_tokens` never sent as `num_predict` (response length unbounded by config); `ollama/models.py` `_DEFAULTS` hardcodes `"llama3.1"` in code. | Options dict built without the token cap; fallback defaults duplicated from YAML. | D |
| D28 | P2 | **Evidence blob integrity**: `EvidenceRegistry.append_blob` swallows store exceptions — a finalized record can cite a blob hash whose bytes were never persisted. | Blob persistence treated as best-effort inside an otherwise strict registry. | F |

**Explicitly refuted suspicions** (checked, not defects): similarity
empty-corpus behavior (honest `skipped`/`None`); any caching of tests/grades
across runs (none exists); correctness scoring from non-`trusted` tests (code
path is trusted-only); API-key concepts / external inference (none anywhere);
report/engine reconciliation (sound, tolerances verified); artifact traversal
via run ids or artifact names (regex + allowlist hold); persistence
immutability (enforced in the store, tested).

---

## Resolution (post-remediation, Phases B–N, 2026-10-20)

Every defect above was remediated in the phase the register assigned.
Statuses: **FIXED** = implemented and covered by the cited test/evidence
on this VM. The pre-fix matrix and register above are unchanged — this
section is the after-state.

| ID | Resolution | Phase | Evidence |
|----|-----------|-------|----------|
| D1 | **FIXED** — Landlock default is now a narrow allowlist (run dir + system lib dirs + `/dev/null`); reads of `/etc/shadow`, SSH keys, and planted secrets outside the run dir are denied and tested. | B | `tests/security/test_read_isolation.py`, `test_symlink_escape.py` |
| D2 | **FIXED** — `preexec_fn` eliminated; a dedicated single-threaded launcher process applies limits/namespaces/Landlock/seccomp, then execs. Children drop to uid/gid 65534 whenever the app runs as root, so `RLIMIT_NPROC` binds. | B | `tests/security/test_identity.py`, `test_policy_fail_closed.py` |
| D3 | **FIXED** — network namespace is mandatory: launcher fails (exit 125) before exec if `unshare(CLONE_NEWNET)` fails; enforcement is reported from the launcher, not probes. | B | `tests/security/test_network_mandatory.py` |
| D4 | **FIXED** — restrictive seccomp BPF allowlists installed (separate `runtime`/`compiler` profiles, deny-by-default EPERM); the capability probe now installs the real filter and requires `socket()` → EPERM before reporting `seccomp: true`. | B | `tests/security/test_seccomp.py` |
| D5 | **FIXED** — thread pool deleted; each analyzer runs in its own subprocess with a hard per-analyzer deadline (TERM → grace → KILL its process group) plus an overall evaluation timeout (default 1800 s); a killed worker's sandbox descendants are reaped via a `/proc` descendant scan. | E | `tests/integration/test_coordinator_deadlines.py` (hang, crash, malformed output, orphan-kill, overall deadline) |
| D6 | **FIXED** — scoring policy **1.1**: zero eligible evidence → grade `None`, status `incomplete`, explicit reason; a confirmed zero (eligible metrics scored 0) still yields 0.0. The unit test that pinned the old 0.0 was updated to pin the new semantics. | C | `tests/unit/test_scoring_properties.py` |
| D7 | **FIXED** — `MetricScore`/`ReportMetric` carry both `configured_weight` and `effective_weight`; `validate_report` reconciles against effective weights and checks configured weights against the rules; HTML shows both columns. | C | `tests/unit/test_report.py`, `tests/integration/test_ui_honesty.py` (JSON↔HTML identity) |
| D8 | **FIXED** — `ReferenceSolutionOracle.prepare()` tracks example coverage; a reference with no spec examples reproduced is capped at `provisional` with the limitation recorded; validated mismatches downgrade as before. | D | `tests/integration/test_generation_oracle.py` |
| D9 | **FIXED** — the `ctx.reference_executor` assess path now compares the reference's output against the candidate's expected output through the shared comparison helper; no trust without comparison anywhere. | D | `tests/integration/test_generation_oracle.py`, `tests/security/test_oracle_sandbox.py` |
| D10 | **FIXED** — `BudgetTracker` is enforced: token usage accumulates from gateway responses (checked before each generation round), and one shared per-run execution budget is charged *before* oracle/correctness executions; exhaustion stops work and is disclosed, never silently scored. | D | `tests/integration/test_generation_oracle.py`, `tests/unit/test_analyzers_correctness.py` |
| D11 | **FIXED** — complexity with no required class is `inconclusive` (score `None`, limitation "no required complexity class specified"); the manufactured 1.0 default is removed. | G | `tests/unit/test_analyzers_complexity.py` |
| D12 | **FIXED** — memory-safety results disclose both the disabled address-space limit (with the compensating ASan RSS cap) and `detect_leaks=0` (leaks not checked); a submission that fails the sanitizer build is `failed`, distinct from toolchain-`unsupported`. | G | `tests/unit/test_analyzers_memory.py` |
| D13 | **FIXED (code)** — cppcheck now runs through the sandbox executor (compiler profile), never as a bare host subprocess, and results state cppcheck's true outcome. *Caveat: cppcheck is not installed on this VM, so the sandboxed cppcheck path is implemented but unexercised here.* | B/G | `tests/unit/test_analyzers_static.py` (outcome states via stub executor) |
| D14 | **FIXED** — an empty LLM review, or one missing rubric dimensions, is `inconclusive`; no more `completed` 0.0 or subset averaging. | G | `tests/unit/test_analyzers_readability.py` |
| D15 | **FIXED** — correctness classifies each run `run_ok` vs `infra_error`; infra-failed tests are excluded from the pass/fail fraction, counted in `infra_errors`, and warned about; all-infra → `inconclusive`. | G | `tests/unit/test_analyzers_correctness.py` |
| D16 | **FIXED** — the spec-degradation warning is attached only when the spec is genuinely missing after extraction; successful LLM runs no longer carry the false "no specification available" limitation. | E | `tests/integration/test_orchestration.py` (asserted both directions) |
| D17 | **FIXED** — uploads stream in 64 KB chunks to a temp file with the cap enforced during the write (413 the moment it is exceeded), plus an ASGI guard bounding concurrent uploads (429) and total in-flight bytes; temp files are removed on every failure path. | F | `tests/integration/test_api_hardening.py` |
| D18 | **FIXED** — YAML rules uploads keep their `.yaml` extension and work end-to-end (upload → list → evaluation); the silent-rename bug is gone. | F | `tests/integration/test_api_hardening.py` |
| D19 | **FIXED** — client-visible error text is sanitized (no absolute paths, no tracebacks; structured `{error: {code, message}}`); `meta.json` writes are locked + atomic; missing upload files return 404 instead of 500. | F | `tests/integration/test_api_hardening.py` |
| D20 | **FIXED** — manifests are written at finalize with terminal state + `finished_at`; version strings come from constants/probes (`POLICY_VERSION`, `SANDBOX_VERSION`, probed compiler, real Python version); generation provenance records the actual model id + prompt version (or honestly "none (llm unavailable)"). Demo artifacts were regenerated under the fixed code. | E | `tests/integration/test_orchestration.py`; `docs/demo/` artifacts |
| D21 | **FIXED** — `verify_feedback` also validates cited source locations against the submission's line count and metric-score claims against the scoring engine's per-metric results; violations feed the existing bounded-regeneration/fallback path. | I | `tests/unit/test_feedback_verification.py` |
| D22 | **FIXED** — `.env.example` lists exactly the variables the code reads (`OLLAMA_URL`, `ARTIFACT_DIR`, `AUTOGRADER_SANDBOX_POLICY`, `AUTOGRADER_FEEDBACK_LLM_CHECK`); the `feedback_check` prompt/task is wired as an optional, default-off LLM audit that can only trigger regeneration; stub packages `app/evidence/`, `app/agents/` and the empty root dirs are deleted. | G/H/K | `tests/unit/test_feedback_llm_check.py`; `.env.example` |
| D23 | **FIXED** — `requirements.lock` regenerated portable (42 pinned packages, no local paths, no editable self-reference); `pytest` moved to the dev extra; templates/static declared as package data. Proven: fresh venv installed purely from the lock runs the unit suite green (236 passed). | K | `docs/INSTALL.md` (documented sequence + proof) |
| D24 | **FIXED** — documentation re-walked against the remediated code: `docs/SANDBOX.md` rewritten in Phase B; `docs/ACCEPTANCE.md` rewritten with evidence-bound statuses (this pass); `docs/ARCHITECTURE.md`, `docs/HANDOFF.md`, `docs/INSTALL.md`, `docs/LIMITATIONS.md`, `README.md` corrected. No unconditional PASS remains without an evidence pointer. | B–N | `docs/ACCEPTANCE.md` (status key + per-item evidence) |
| D25 | **FIXED** — DEMO.md now prints two complete, runnable commands; both were re-executed verbatim on 2026-10-20 and reproduce the documented outcomes exactly (statuses, grade `None`, 16 uncertainty entries, 7 limitations, per-analyzer statuses). | N | `docs/DEMO.md` + regenerated `docs/demo/` artifacts |
| D26 | **FIXED** — SQLite WAL + `busy_timeout`; the API shares one `RunStore` with the workflow; artifact writes roll back on insert failure and orphaned files are reconciled at startup; `recover_interrupted_runs()` fails runs left non-terminal by a restart. | F | `tests/unit/test_persistence.py`, `tests/integration/test_api_hardening.py` (restart test) |
| D27 | **FIXED** — the gateway sends the configured `max_tokens` as Ollama `num_predict`; the residual `_DEFAULTS["model"] = "llama3.1"` literal in `ollama/models.py` is unreachable, because the loader raises unless the YAML task entry itself names a model (which then overrides the default). | D | `tests/integration/test_ollama_gateway.py` (request body asserts `num_predict`) |
| D28 | **FIXED** — `append_blob` writes the blob, reads it back, and cites the hash of the persisted bytes; failures raise instead of minting a record; `verify_blob`/`verify_run_artifacts` detect tampering or deletion. | F | `tests/unit/test_evidence_blob.py` |

### Known residuals (not in the D-register; recorded honestly)

- **Specification truncation is still silent**: `specification/agent.py`
  renders at most 12 000 chars of the assignment into the extraction
  prompt and does not record the truncation in
  `extraction_diagnostics` (a matrix-row minor from the pre-fix audit,
  never promoted to the register). A very long assignment can therefore
  yield a thinner spec than the document supports, with no signal in the
  report.
- **Cross-worker exec-budget overshoot**: analyzer workers enforce the
  execution budget against their seeded snapshot and the coordinator
  reconciles deltas afterwards, so concurrent workers can collectively
  overshoot before reconciliation. Today only correctness charges
  executions among analyzers, so the analyzer phase has a single
  consumer; the caveat is documented in code and in
  `docs/LIMITATIONS.md`.
- **Modularity blind spot**: macro-*generated* functions are invisible
  to the regex parser entirely (not even counted as candidates), so
  `parse_confidence` can read 1.0 while functions are missed. The
  analyzer's limitation text covers this approximately; a real fix needs
  a real C parser.
- **cppcheck / real-LLM / WSL2 behavior**: unchanged from the pre-fix
  "Environment-blocked" table — those items remain UNVERIFIED on this
  VM and are gated on the target machine (see `docs/OLLAMA.md`).
