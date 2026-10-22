# Known Limitations

Concrete, not generic. Each limitation names a specific scenario where a
verdict would be unreliable, and what to do about it.

## 1. No trusted oracle → no confirmed correctness verdict

**Unreliable-verdict scenario:** the LLM generates 12 candidate tests for a
submission, but every candidate's expected output lands `provisional` or
`inconclusive` (e.g. the assignment's worked examples are ambiguous and no
instructor reference was provided). The correctness analyzer then reports
`inconclusive`, the run is marked `incomplete`, and **no grade is issued** —
even if the student's program is actually correct.
**Mitigation:** provide a Tier-1 oracle: an instructor reference solution
(`oracle.reference_solution_path`) or expected outputs
(`oracle.expected_outputs_path`), or write the assignment's worked examples
unambiguously so the Tier-2 spec-example oracle can anchor. The report's
`limitations` always state which tier was missing.

## 2. Timing measurements never prove Big-O

**Unreliable-verdict scenario:** a student's O(n²) sort passes the
correctness tests and the timing leg shows "fast" on the small generated
inputs (n ≤ 1000, where n² is still cheap). A timing-only read would call
it O(n log n) — wrongly.
**Mitigation:** the complexity analyzer scores from **static inference**
(loop-nesting depth, recursion detection) against `rules.complexity.required`;
timing is recorded for corroboration only and every report carries the
limitation "timing measurements are recorded for corroboration only and are
never used as proof of a Big-O class". State the required class in the
rules file.

## 3. LLM non-determinism moves evidence between runs

**Unreliable-verdict scenario:** two runs of the same submission produce
slightly different candidate test sets; one run's borderline test reaches
`trusted` and the other stays `provisional`, shifting the correctness score
by ~3 points and the final grade from 78.4 to 75.9.
**Mitigation:** temperature 0 + fixed seeds per task; the trust gate caps
how far a flaky guess can move the score (it can only ever become
`provisional`, never a confirmed failure); scoring itself is deterministic
given evidence. Acceptance bar: max deviation ≤ 7.0 across repeated runs
(measured 0.00 with deterministic stand-in; real-LLM measurement belongs on
the Ollama machine — see [docs/VARIANCE.md](docs/VARIANCE.md)).
**Per-assignment cache (2026-10-22):** the specification and the
oracle-validated test set are now generated once per assignment and
reused for every student of that assignment, so this variation is paid
once per assignment generation rather than once per student — every
student of an assignment is graded against the *same* test set. That
removes per-student test-set luck and replaces it with the shared-fate
risk documented in §11.

## 4. Similarity is resemblance, not authorship proof

**Unreliable-verdict scenario:** two students independently write the
obvious 10-line solution to "add two integers"; token-normalized shingle
similarity is 0.92, above the flag threshold. Treating the flag as a
cheating verdict would punish independent correct work.
**Mitigation:** the analyzer **flags for human review only** — the code,
report language, and docs never claim authorship or LLM-authorship
detection (asserted by a unit test). Instructors should use flags as triage,
compare flagged pairs by hand, and set the corpus/threshold per assignment
(`rules.similarity`).

## 5. Memory-safety runs do not check leaks, and ASan runs lift the address-space limit

**Unreliable-verdict scenario:** a submission leaks 50 MB per test input
but never touches freed or out-of-bounds memory. The memory-safety
analyzer reports it clean — a reader who assumes "memory safety" includes
leak checking is misled.
**Why:** ASan runs with `detect_leaks=0` (leak reports made the
instrumented runs unreliable in this sandbox), and ASan requires a huge
virtual address space, so `RLIMIT_AS` is disabled for sanitizer runs;
physical memory is still bounded by ASan's `hard_rss_limit_mb` cap plus
the wall/CPU/output limits. Both facts are disclosed in every completed
memory-safety result's `limitations`.
**Mitigation:** treat the metric as "memory *errors* + undefined
behavior", not leak detection. If leak verdicts matter, run valgrind
memcheck separately — it is not integrated.

## 6. Resource accounting is rlimit-based — no cgroup v2

**Unreliable-verdict scenario:** on a shared host, an instructor wants a
hard per-run CPU-seconds budget across *all* of a run's processes,
including short-lived grandchildren that each stay under their own
per-process rlimits. The aggregate can exceed what a single rlimit
reading suggests.
**Why:** cgroup v2 is not delegated on the development VM (and may not
be on the target either), so the sandbox enforces per-process rlimits
(CPU, address space, file size, process count), a wall-clock timeout,
output caps, and process-group kills — containment is enforced and
tested, but there is no cgroup-level aggregate accounting.
**Mitigation:** the configured concurrency bounds (≤2 API evaluation
workers, ≤4 analyzer processes per evaluation, per-test limits from the
rules file) bound the aggregate in practice. On a host with delegated
cgroup v2, per-run accounting would still need to be wired — it does
not happen automatically.

## 7. The execution budget is reconciled across analyzer workers, not globally atomic

**Unreliable-verdict scenario:** the rules set a tight `exec_budget`
and, in a future configuration where more than one analyzer charges
sandbox executions concurrently, the run executes a few more tests than
the budget before the coordinator reconciles the workers' counters.
**Why:** each analyzer worker enforces the budget strictly against its
seeded snapshot; the parent sums the deltas afterwards. Today only the
correctness analyzer charges executions, so the analyzer phase has a
single consumer and the budget is exact in practice.
**Mitigation:** keep execution-charging analyzers singular per phase, or
treat the budget as a bound with small concurrent slack. The budget
*does* hard-stop generation rounds and oracle/correctness execution
within each consumer (checked before every operation).

## 8. Seccomp policy is x86_64-only; other architectures refuse to run

**Unreliable-verdict scenario:** the app is deployed on an ARM64 Linux
host (e.g. an ARM cloud VM or Apple-silicon WSL). The syscall allowlist
is an x86_64 table; rather than install a wrong filter, the launcher
refuses and evaluations fail closed with `SandboxUnavailable`.
**Mitigation:** deploy on x86_64 Linux (the documented target), or add a
reviewed aarch64 syscall table — do not loosen the filter to "make it
run".

## 9. Privilege drop exists only when the app itself runs as root

**Unreliable-verdict scenario:** on a normal (non-root) deployment,
sandboxed student code runs with the same HOST uid as the app — inside
its own user namespace (uid 0 inside, the app uid outside), with
Landlock, seccomp, and the network namespace containing it (tested in
exactly that configuration by
`tests/security/test_nonroot_identity.py`). But there is no second
HOST uid, so anything the app user can read *through a sandbox
escape* is exposed.
**Why:** an unprivileged process cannot setuid to another user; the
user-namespace mapping is the strongest host-identity change available
to it. The dedicated-identity setuid drop (uid/gid 65534 by default,
configurable via `AUTOGRADER_SANDBOX_POLICY`) applies when the app
runs as root, which is the configuration the D2 identity test
exercises.
**Mitigation:** run the app as root in a dedicated VM/container if the
two-host-uid boundary matters, or accept the namespace model with the
Landlock allowlist as the read barrier (the planted-secret regression
test proves that barrier in the non-root configuration — and proves
the run is refused outright on hosts that forbid user namespaces).
The enforcement report always states the strategy and the actual
(outer) child uid — it never claims a drop that didn't occur.

## 10. Real-model behavior is unverified (generation quality, variance, reviews)

**Unreliable-verdict scenario:** an instructor reads the 0.00 variance
figure or the mock-proven pipeline and assumes a real llama3.1 will
generate equally stable, high-quality tests on their machine. The real
model may produce weaker candidates, more `provisional` verdicts, and
grade movement approaching the ≤ 7.0 bar — none of it measured yet.
**Why:** this development VM has no Ollama; every LLM path is proven
only against labeled test doubles, and the 4 real tests in
`tests/integration/test_ollama_real.py` skip explicitly here.
**Mitigation:** before relying on LLM-driven grades, run the gate
procedure in [docs/OLLAMA.md](docs/OLLAMA.md) on the target machine and
record real variance in [docs/VARIANCE.md](docs/VARIANCE.md). Until
then, treat LLM-dependent acceptance items as BLOCKED–ENVIRONMENT
(`docs/ACCEPTANCE.md` #5, #17, #22).

## 11. A cached test set is shared by every student of the assignment

**Unreliable-verdict scenario:** the single generation run for an
assignment produces a test whose expected output is subtly wrong, yet a
Tier-1/Tier-2 oracle path marks it `trusted`. That test is then reused
for *every* student of the assignment: a correct submission can lose
the same points in run after run until someone regenerates the set.
(Under the previous generate-per-run policy, such a flaw typically
affected a single run.)
**Why the cache is still safe to use:** only oracle-validated sets are
cached — a generation is written only when spec extraction succeeded
and at least one test reached `trusted`; a failed or empty generation
writes nothing, so the next run retries instead of inheriting a bad
set. Reuse never promotes an oracle status, and every reusing report
names the producing run and its generation time in its notes and in
the manifest diagnostics, so a bad set is traceable to exactly one
generation.
**Mitigation:** if a cached set is suspect, regenerate it —
`--force-regenerate` on the CLI, or
`test_generation.reuse_cached_testset: false` in the rules — and
prefer Tier-1 anchors (an instructor reference solution or expected
outputs) for high-stakes assignments, since Tier-1 evidence is what
makes a cached set worth trusting repeatedly.

## Platform & process caveats

- **16 GB target is arithmetic, not a load test.** Measured on this VM:
  API server idle ≈ 61 MB RSS; one full (degraded) evaluation ≈ 178 MB
  peak process-tree RSS. Configured bounds (2 evaluation workers, ≤4
  analyzer processes each, per-test memory limits from rules) keep the
  app-side total far under 16 GB on paper, and Ollama's model memory
  (~4.9 GB for llama3.1) is separate — but no full 16 GB-machine load
  test has been performed.
- **CI is written, not yet observed.** `.github/workflows/ci.yml`
  (deterministic suite, security suite as its own job, lint) is
  YAML-validated but has never run on GitHub Actions from this
  environment; runner-specific capability differences may surface in
  the security job on its first run.
- **WSL2 target behavior is unmeasured.** The sandbox's mandatory
  mechanisms (Landlock, netns, seccomp) are verified on this VM's
  kernel 7.0; the WSL2 kernel on the target machine may report
  different capabilities, in which case the app fails closed rather
  than degrading silently.
