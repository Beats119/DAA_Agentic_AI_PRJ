# Sandbox security boundary

The sandbox (`autograder/app/sandbox/`) executes untrusted student C code
(and the untrusted compilation step itself). It is native Linux, no
containers, no root required.

## Enforcement layers (every run)

| # | Layer | Mechanism | Degrades? |
|---|-------|-----------|-----------|
| 1 | Fresh temp dir as cwd | `mkdtemp`, removed in `try/finally` even on timeout/crash | No |
| 2 | Resource limits | `RLIMIT_CPU` / `RLIMIT_AS` / `RLIMIT_FSIZE` / `RLIMIT_NPROC` / `RLIMIT_STACK` in child `preexec`; `RLIMIT_CORE=0` | **Fail closed** (`ensure_available`) |
| 3 | Process-group kill | `start_new_session=True` + `killpg(SIGKILL)` on wall timeout; verified by test (forked sleeper dies too) | **Fail closed** (`ensure_available`) |
| 4 | Network namespace | `unshare(CLONE_NEWNET)` when the kernel allows | Best effort, honestly reported |
| 5 | Filesystem containment | **Landlock ABI v1** (unprivileged, via syscalls): read+execute on `/`, full access only under the run dir, read/write on `/dev/null`; writes anywhere else (`/tmp`, home, `../../escape`) fail with `EACCES` | Per-run fail closed: child exits before `exec` if it cannot be applied |
| 6 | Output caps | select-loop pump, byte caps on stdout/stderr, `output_truncated` recorded | No |
| 7 | Sanitized environment | minimal env (`PATH`, `LANG`); stdin via pipe; no `shell=True` anywhere; argv lists only | No |

`ExecutionResult.enforcement` reports per run exactly what was established:
`resource_limits`, `process_group_kill`, `network_isolated`,
`filesystem_hardened`, `temp_dir`. Nothing is claimed that was not measured.

## Capability probe (this build machine)

```
user_namespaces:   False  (creatable, but uid_map not writable as uid 65534 —
                           honestly reported, not used)
network_namespace: True
seccomp:           True   (filter mechanism present; not currently used)
cgroup_v2:         False  (mounted but no controllers delegated)
resource_limits:   True
landlock_abi:      1
compiler:          /usr/bin/gcc
```

User namespaces were deliberately **not** relied on: on hosts where the id
map cannot be written the child would run as the overflow uid with confusing
semantics. Landlock + netns + rlimits give stronger, verifiable guarantees
without them.

## Landlock application details (Phase 6 fix)

`apply_landlock()` runs in the sandboxed child **after** `RLIMIT_NPROC` is
set. The ABI probe it depends on (`landlock_abi_version()`) is therefore
**fork-free by design**: it calls `landlock_create_ruleset` directly via
`ctypes` (which merely allocates a ruleset FD) instead of forking a probe
child. An earlier design forked the probe after the nproc limit was
applied; under load that fork could fail with `EAGAIN` and wrongly refuse
execution. The current code cannot hit that race — verified by the
fork-bomb containment test running concurrently with Landlock application.

## Fail-closed behavior

- `SandboxExecutor.ensure_available()` raises `SandboxUnavailable` when
  `RLIMIT` or `killpg` is unavailable — untrusted code is never executed.
- If Landlock cannot be applied in a particular child, the child exits with
  code 125 **before** `exec`; the parent raises `SandboxError` and refuses
  the result. The untrusted binary never runs.
- A reference solution that fails to compile, crashes, or times out yields
  `inconclusive` oracle verdicts — never trusted, never silently skipped.

## Residual risks (documented, not hidden)

- **No CPU-time accounting via cgroups**: `RLIMIT_CPU` + wall timeout bound
  runaway programs, but precise per-run CPU billing needs cgroup v2
  delegation (absent here).
- **No syscall filtering**: seccomp filters are not installed; a program can
  invoke any syscall the Landlock/rlimit/netns policy permits. Landlock
  covers the filesystem attack surface, which is the relevant one for
  grading.
- **Side channels** (timing, `/proc` inspection): not mitigated. Student
  programs can observe each other's *existence* via `/proc` but cannot read
  or write each other's files (Landlock) — and runs are sequential per
  worker in the orchestrator.
- **Landlock is required** for the filesystem guarantee: on kernels without
  it (`landlock_abi_version() is None`) runs are refused. This is
  intentional — see above.
- The sandbox runs with the invoking user's privileges. It contains what
  the *program* can do, not what the *operator* can do.

## WSL2 (target machine) expectations

Run `python -m autograder.app.sandbox.capabilities` on the target host.
Expected on a standard WSL2 kernel (≥ 5.15):

- `resource_limits: True`, `compiler: <gcc path>` — required.
- `landlock_abi: 1` — required for filesystem containment (Microsoft's WSL2
  kernels enable `CONFIG_SECURITY_LANDLOCK`; verify with the probe).
- `network_namespace: True` — expected; if `False`, runs proceed with
  `network_isolated: False` recorded in every result.
- `user_namespaces` / `cgroup_v2` — informational only, not required.

## Running the adversarial tests

```
pytest tests/security -q
```

Eleven test modules, all with real hostile binaries: infinite loop (+forked
sleeper, proves `killpg`), memory bomb (`RLIMIT_AS` → `SIGSEGV`), output
flood (byte cap), fork bomb (`RLIMIT_NPROC` + tree kill), filesystem escape
(Landlock denies `/tmp` and `../../escape`), segfault (signal recorded),
compiler failures (structured diagnostics + path-traversal rejection),
network isolation (honest reporting), fail-closed (degraded host refuses to
run), capabilities probe honesty, oracle-under-sandbox. No mocks — every
assertion requires the real mechanism to work.
