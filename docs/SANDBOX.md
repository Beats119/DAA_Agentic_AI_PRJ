# Sandbox security boundary

The sandbox (`autograder/app/sandbox/`) executes untrusted student C code
(and the untrusted compilation step itself). It is native Linux, no
containers. Since the Phase-B overhaul its trust model is an **enforced,
fail-closed policy**: every layer below is mandatory by default, is
established by a dedicated launcher process, and is reported back as a
measured actual — if any mandatory layer cannot be established, the run
is refused and the untrusted program never executes.

## Policy (`sandbox/policy.py`)

`SandboxPolicy` is a Pydantic model. It can be overridden with a JSON file
named by the `AUTOGRADER_SANDBOX_POLICY` environment variable; an invalid
or unreadable policy file raises `SandboxPolicyError` — it never silently
falls back to defaults.

| Setting | Default | Meaning |
|---|---|---|
| `mandatory` | `resource_limits`, `landlock_fs`, `network_namespace`, `seccomp_filter`, `no_new_privs` | Capabilities that MUST be established for every run, or the run is refused |
| `sandbox_uid` / `sandbox_gid` | `65534` / `65534` (`nobody`/`nogroup`) | Dedicated unprivileged identity the child drops to when the app runs as root |
| `drop_privileges_when_root` | `true` | Perform the identity drop when euid == 0 |
| `fs_read_paths` | `/lib`, `/lib64`, `/usr/lib`, `/usr/lib64` | The ONLY host directories readable/executable by default (runtime libraries + loader). Never `/`, never `/etc`, never a home directory |
| `seccomp_profile` | `runtime` | Default syscall profile (`runtime` \| `compiler`) |

Optional (non-mandatory) extras: process-group kill (always on, checked
by `ensure_available`), extra per-run `read_paths` / `writable_paths`
grants (compiler source/output directories), and the ASan-only
`unlimited_address_space` opt-in (see Residual risks).

A policy mandating a capability name the sandbox does not know makes
`ensure_available` raise `SandboxUnavailable`. Disabling a mandatory
mechanism via the constructor flags (`use_landlock=False`,
`use_network_namespace=False`) likewise raises instead of weakening runs.

## Launcher design (`sandbox/launcher.py`) — no `preexec_fn`

The parent process NEVER uses `preexec_fn`: applying namespaces, rlimits
and LSM rules in the `fork()` of a multithreaded application (the
coordinator and API run thread pools) is documented by CPython as unsafe.

Instead, every run spawns a fresh, single-threaded launcher:

```
python -m autograder.app.sandbox.launcher <spec.json>
```

The parent passes the spec path as argv and a status pipe via
`pass_fds`; the child starts in its own session (`start_new_session=True`)
with cwd set to the run directory. The launcher then, in order:

0. **Pre-opens every Landlock rule fd** (`O_PATH`) while still fully
   privileged — after the identity drop, path-based opens could fail on
   ancestors the sandbox uid cannot traverse; open fds are immune.
1. `unshare(CLONE_NEWNET)` — network namespace (mandatory).
2. rlimits: `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`, `RLIMIT_NPROC`,
   `RLIMIT_STACK`, `RLIMIT_CORE=0`.
3. Identity drop (when euid == 0 and policy allows): `setgroups([])`,
   `setgid(65534)`, `setuid(65534)`, then verifies `getuid() != 0`.
   Dropping to a real unprivileged uid is also what makes
   `RLIMIT_NPROC` enforceable (it is not enforced for uid 0). When the
   app runs unprivileged, the child keeps the invoking uid and the
   status report says `uid_dropped=false, reason="not root"`.
4. `prctl(PR_SET_NO_NEW_PRIVS, 1)`.
5. Landlock: ruleset handling **all** filesystem access rights the
   kernel mediates; only the allowlist below gets rules (mandatory).
6. Seccomp: installs the profile's BPF allowlist (mandatory).
7. Writes ONE JSON status line to the status pipe —
   `{ok, netns, landlock, seccomp, resource_limits, no_new_privs, uid,
   uid_dropped}` — then `execve`s the target.

Any mandatory failure writes `{ok: false, failed_step, error}` and exits
**125** before exec. The parent reads the status line BEFORE pumping I/O;
a missing/invalid status, exit 125, or a status field contradicting the
mandatory policy raises `SandboxError` and kills the process group.
`ExecutionResult.enforcement` is built from the launcher's report —
actuals, not probes:

`resource_limits`, `process_group_kill`, `network_isolated`,
`landlock_fs`, `seccomp_filter`, `uid`, `uid_dropped`, `temp_dir`.

## Filesystem allowlist (Landlock, default-deny)

Landlock is default-deny for reads AND writes once restricted. Per run,
the complete grant set is:

| Path | Rights |
|---|---|
| The per-run temp dir (cwd) | all (read/write/create/execute) |
| `fs_read_paths` (`/lib`, `/lib64`, `/usr/lib`, `/usr/lib64`) | read file, read dir, execute |
| `/dev/null` | read + write |
| `/dev/zero`, `/dev/urandom` | read |
| The target binary, when it is a system tool (`/usr`, `/bin`, `/sbin`, `/lib*`, `/opt`) | read + execute on that one file |
| Per-run `writable_paths` (compiler output dir) | all |
| Per-run `read_paths` (compiler source dir, `/usr/bin`, `/usr/include`, `/usr/libexec`) | read file, read dir, execute |
| The launcher's own `/proc/self` | read (ASan/procfs helpers) |

Non-system binaries (student programs) are **copied into the run dir**
by the parent and invoked as `./prog`, so no host path to them is ever
granted. Symlinks cannot escape: Landlock evaluates the resolved target
at access time (proven by `test_symlink_escape.py`). Reading
`/etc/shadow`, `/etc/hostname` (world-readable by DAC — only Landlock
denies it), SSH keys, `.env` files, or application files from inside the
sandbox fails (proven by `test_read_isolation.py`).

DAC note: Landlock sits on top of discretionary permissions — both must
permit. When the child drops to the sandbox uid, the executor chowns the
run dir and writable output dirs to it, ensures traverse-only (`o+x`)
permission on ancestors of granted paths, and ensures granted read files
are readable. Compilation additionally *stages* the source into the
output directory (with `-ffile-prefix-map` so diagnostics keep the
original path), because a root-launched compiler child running as
`nobody` may be unable to read the original source path at all.

## Seccomp profiles (`sandbox/seccomp.py`)

Classic-BPF allowlists built with ctypes (no libseccomp dependency):
validate `arch == AUDIT_ARCH_X86_64` (mismatch → `KILL_PROCESS`), compare
the syscall number against the profile, default action
`SECCOMP_RET_ERRNO | EPERM`. x86_64 only — on other architectures the
launcher refuses to run rather than install a wrong table.

- **`runtime`** — stdio/file I/O on already-openable paths, memory
  management, signals, time, futex, identity *queries*, loader needs
  (`arch_prctl`, `rseq`, …), and process creation
  (`fork`/`vfork`/`clone`/`clone3`/`wait4`/`waitid`), plus
  `madvise`/`mremap`/`clock_nanosleep` (glibc malloc/`sleep`),
  `readlinkat` (ASan symbolizer) and `symlink`/`symlinkat` (link
  creation; Landlock judges targets). DENIED (returns EPERM): the whole
  socket family, `ptrace`, `mount`/`umount`, `unshare`, the
  `setuid`/`setgid` family, `bpf`, `io_uring_setup`, and everything not
  listed. Process creation is allowed deliberately: fork-bomb
  containment comes from `RLIMIT_NPROC` (now effective — the child is
  not root), CPU/wall limits, and process-group kill, all proven by
  tests that require a real forked child to exist.
- **`compiler`** — `runtime` ∪ gcc's needs: `execveat`, `chdir`,
  `mkdir`, `unlink(at)`, `rename`, `link`, `chmod`, `utimensat`,
  `truncate`/`ftruncate`, `fallocate`, `copy_file_range`, `sendfile`,
  `getrusage`, `kill`, `setpgid`, `alarm`, `fcntl`, `gettimeofday`,
  `getrlimit`. Used for gcc AND for cppcheck (see below).

Proven by `test_seccomp.py`: `socket()` fails with `errno == EPERM`,
`ptrace` fails with EPERM, an ordinary stdio/malloc/scanf program works,
and ASan+UBSan binaries run, report a use-after-free, and exit cleanly
on clean input — all under the runtime profile.

## Compilation and tool execution

- gcc runs through the executor with the `compiler` profile, its source
  staged into the output dir, `TMPDIR` inside the output dir.
- **cppcheck** (static analysis) also runs through the executor
  (`compiler` profile, read access to the source dir + cppcheck's cfg
  dir) — it parses untrusted source, so it is never run as a bare host
  subprocess. If no executor is available to the analyzer, cppcheck is
  treated as unavailable rather than run unsandboxed.

## Capability probe (measured on this build machine)

```
$ python -m autograder.app.sandbox.capabilities
user_namespaces:   false  (not used by design)
network_namespace: true
seccomp:           true   (the REAL runtime filter was installed in a probe
                           child and socket() was denied with EPERM while
                           getpid() still worked — mechanism-presence alone
                           is NOT reported as capability)
cgroup_v2:         false  (mounted, no controllers delegated)
resource_limits:   true
landlock_abi:      8      (the REAL ABI reported by the kernel via
                           LANDLOCK_CREATE_RULESET_VERSION)
landlock_driver_abi: 1    (rules are granted with the ABI-v1 rights subset;
                           the handled mask nevertheless covers ALL FS
                           rights the kernel mediates, so nothing the
                           kernel knows about escapes mediation)
compiler:          /usr/bin/gcc
```

`ensure_available()` additionally runs a **functional self-test**: a real
`cat` echo round-trip through the full launcher stack (falling back to
`/usr/bin/true`), verifying the returned enforcement report. Probes alone
do not authorize execution.

## Kernel / Landlock requirements

- Linux with Landlock enabled, ABI ≥ 1 (any kernel ≥ 5.13 with
  `CONFIG_SECURITY_LANDLOCK=y`). The real ABI is probed, not assumed —
  this machine's kernel reports ABI **8**.
- Network namespaces creatable by the launching user (root, or
  `CAP_SYS_ADMIN`, or unprivileged netns where enabled).
- x86_64 (seccomp tables are architecture-specific).
- If any of these are missing, `ensure_available` raises
  `SandboxUnavailable` and NOTHING executes — there is no degraded mode.

## Fail-closed behavior (summary)

- Unknown mandatory capability in policy → refuse.
- Launcher mandatory step failure → exit 125 before exec, parent raises
  `SandboxError`, target never runs (proven by marker-file tests in
  `test_policy_fail_closed.py`, including a direct launcher-level test).
- Invalid policy file → `SandboxPolicyError` at load.
- Invalid seccomp profile for a run → `SandboxError` before spawn.

## Residual risks (documented, not hidden)

- **rlimits instead of cgroups**: CPU/wall/memory/process limits are
  per-process rlimits, not cgroup accounting. A program can consume up to
  its per-process limits in *each* forked child (bounded by
  `RLIMIT_NPROC` and the wall clock). Precise per-run accounting needs
  cgroup v2 delegation, which this host does not provide
  (`cgroup_v2: false`). This is a resource-accounting gap, not a
  containment gap: nothing escapes the filesystem/network/syscall policy.
- **ASan address-space opt-in**: sanitizer binaries need
  `RLIMIT_AS = ∞` (`unlimited_address_space`, used ONLY for instrumented
  runs); physical memory is then capped by ASan's own
  `hard_rss_limit_mb`. Landlock/seccomp/netns/identity still apply.
- **Allowed-syscall surface**: the runtime profile necessarily allows
  ordinary syscalls; a kernel exploit in an allowed syscall is out of
  scope for a userspace sandbox (this is the standard seccomp caveat).
- **Side channels** (timing, `/proc` existence checks): not mitigated.
  Runs are sequential per worker in the orchestrator.
- **Identity when unprivileged**: when the app itself runs as a normal
  user there is no dedicated sandbox uid to drop to; the child keeps the
  invoking uid, contained by Landlock + seccomp + netns. Running the app
  as root on the grading host is what enables the `nobody` drop.

## WSL2 (target machine) expectations

Run `python -m autograder.app.sandbox.capabilities` on the target host.
Required: `resource_limits: true`, `compiler: <gcc path>`,
`landlock_abi: >= 1` (real value, e.g. 5–6 on recent WSL2 kernels),
`network_namespace: true`, `seccomp: true`. If any is missing the
executor refuses to run untrusted code — check Landlock is enabled in
the kernel and that the app runs with permission to create namespaces.

## Running the adversarial tests

```
pytest tests/security -q
```

Seventeen modules, all with real hostile/observing binaries, no mocks of
enforcement: timeout + process-tree kill, memory bomb, output flood,
fork bomb, filesystem escape (writes), **read isolation** (D1),
**symlink escape**, **identity drop** (D2), **mandatory networking**
(loopback + external connects, D3), **seccomp EPERM proofs** (D4),
**policy fail-closed** (bogus mandate, launcher-level refusal with
marker-file proof), compiler failures, crash signals, capabilities
honesty, fail-closed on degraded hosts, oracle-under-sandbox.
