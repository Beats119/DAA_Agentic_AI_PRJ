"""Isolated execution of untrusted binaries.

Defense in depth, applied in this order for every run:

1. **Fresh temp dir as cwd** — per-run directory, removed afterwards even on
   timeout/crash (``try/finally``).
2. **Resource limits** (``resource`` module, set in the child ``preexec``):
   ``RLIMIT_CPU`` / ``RLIMIT_AS`` / ``RLIMIT_FSIZE`` / ``RLIMIT_NPROC`` /
   ``RLIMIT_STACK``, plus ``RLIMIT_CORE=0`` (no core dumps).
3. **Process group** — ``start_new_session=True``; on wall-time timeout the
   whole group dies via ``killpg`` (children spawned by a fork bomb or a
   ``while(1)`` that forks sleepers are reaped too).
4. **Network namespace** (best effort) — ``unshare(CLONE_NEWNET)`` when the
   kernel allows it; honestly reported, never claimed when absent.
5. **Landlock filesystem containment** — the child is confined to read+execute
   on ``/``, full access under its run dir, and read/write on ``/dev/null``.
   Writes anywhere else (``/tmp``, home, ``../../escape``) fail with EACCES.
   If the kernel cannot apply it, the child exits *before* exec and the run
   is refused — never executed uncontained silently.
6. **Output caps** — stdout/stderr are pumped through a select loop with byte
   caps; ``output_truncated`` is recorded instead of buffering gigabytes.
7. **Sanitized environment** — minimal env (``PATH``, ``LANG``); stdin via pipe.

Fail-closed: :meth:`SandboxExecutor.ensure_available` refuses to run when
resource limits or process-group kill are unavailable. Per-run, Landlock
failure also refuses the run (the child never reaches exec).
"""

from __future__ import annotations

import os
import select
import shutil
import signal as _signal
import subprocess
import tempfile
import time
from pathlib import Path

from ..core.schemas import StrictModel
from .capabilities import SandboxCapabilities, probe_capabilities
from .landlock import apply_landlock

SANDBOX_VERSION = "sandbox-1.0"

# Status-pipe flag bits written by the child preexec.
_F_NETNS = 0x01
_F_LANDLOCK = 0x02

# Exit code the child uses when containment setup failed (before exec).
_EXIT_SETUP_FAILED = 125


class SandboxError(Exception):
    """Something went wrong setting up or running the sandbox."""


class SandboxUnavailable(SandboxError):
    """The host cannot provide the minimum isolation bar. Refusing to run."""


class ExecLimits(StrictModel):
    """Per-run limits. Applied via rlimits (child) and the pump loop (parent)."""

    wall_time_s: float = 10.0
    cpu_time_s: float = 30.0
    memory_mb: int = 256
    nproc: int = 32
    output_bytes: int = 1_000_000
    fsize_mb: int = 16
    stack_mb: int = 8
    unlimited_address_space: bool = False
    """When True, RLIMIT_AS is set to infinity instead of ``memory_mb``.

    Needed ONLY for sanitizer-instrumented binaries (ASan reserves ~15 TB
    of *virtual* address space for its shadow memory and cannot start under
    a normal cap). Pair with ``ASAN_OPTIONS=hard_rss_limit_mb=<N>`` so the
    sanitizer itself caps *physical* memory. Never enable for untrusted,
    non-instrumented binaries.
    """


class CompilerLimits(ExecLimits):
    """Generous limits for the (also untrusted) compilation step."""

    wall_time_s: float = 60.0
    cpu_time_s: float = 120.0
    memory_mb: int = 2048
    nproc: int = 64
    output_bytes: int = 5_000_000
    fsize_mb: int = 512
    stack_mb: int = 8


class EnforcementReport(StrictModel):
    resource_limits: bool = False
    process_group_kill: bool = False
    network_isolated: bool = False
    filesystem_hardened: bool = False
    temp_dir: str = ""


class ExecutionResult(StrictModel):
    argv: list[str]
    stdout: str
    stderr: str
    exit_code: int | None
    signal: int | None
    timed_out: bool
    wall_s: float
    cpu_s: float
    peak_mem_kb: int
    output_truncated: bool
    enforcement: EnforcementReport
    sandbox_version: str = SANDBOX_VERSION
    warnings: list[str] = []


def _set_child_rlimits(limits: ExecLimits) -> None:
    import resource

    mem = limits.memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_CPU, (int(limits.cpu_time_s), int(limits.cpu_time_s)))
    if limits.unlimited_address_space:
        resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    else:
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(
        resource.RLIMIT_FSIZE, (limits.fsize_mb * 1024 * 1024, limits.fsize_mb * 1024 * 1024)
    )
    resource.setrlimit(resource.RLIMIT_NPROC, (limits.nproc, limits.nproc))
    resource.setrlimit(
        resource.RLIMIT_STACK, (limits.stack_mb * 1024 * 1024, limits.stack_mb * 1024 * 1024)
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _make_preexec(
    limits: ExecLimits,
    run_dir: str,
    status_w: int,
    want_netns: bool,
    want_landlock: bool,
    writable_paths: list[str],
):
    def preexec() -> None:
        # 1. Resource limits first — if these fail we must not continue.
        _set_child_rlimits(limits)
        flags = 0
        # 2. Network namespace (best effort; honestly reported).
        if want_netns:
            try:
                os.unshare(os.CLONE_NEWNET)
                flags |= _F_NETNS
            except OSError:
                pass
        # 3. Landlock filesystem containment (fail closed per-run).
        if want_landlock:
            try:
                from .landlock import FS_RW_DIR, default_rules

                rules = default_rules(run_dir)
                for p in writable_paths:
                    rules.append((p, FS_RW_DIR))
                apply_landlock(run_dir, rules)
                flags |= _F_LANDLOCK
            except Exception:
                pass
        # Report what was actually established, then either proceed to exec
        # or exit before exec when required containment is missing.
        try:
            os.write(status_w, bytes([flags]))
        finally:
            try:
                os.close(status_w)
            except OSError:
                pass
        if want_landlock and not (flags & _F_LANDLOCK):
            os._exit(_EXIT_SETUP_FAILED)

    return preexec


def _drain_nonblocking(f) -> bytes:
    """Read whatever is immediately available on a non-blocking pipe."""
    chunks = []
    fd = f.fileno()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except (BlockingIOError, OSError):
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


class SandboxExecutor:
    """Runs argv-isolated binaries with the policy above."""

    def __init__(
        self,
        base_dir: str | Path | None = None,
        use_network_namespace: bool = True,
        use_landlock: bool = True,
    ) -> None:
        self.base_dir = Path(base_dir or Path(tempfile.gettempdir()) / "autograder_sandbox")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.use_network_namespace = use_network_namespace
        self.use_landlock = use_landlock
        self._caps = probe_capabilities()

    @property
    def capabilities(self) -> SandboxCapabilities:
        return self._caps

    def ensure_available(self) -> None:
        """Fail closed: refuse to run when the minimum isolation bar is missing."""
        if not self._caps.resource_limits:
            raise SandboxUnavailable(
                "resource limits (RLIMIT) are unavailable on this host — "
                "refusing to execute untrusted code"
            )
        if not hasattr(os, "killpg"):
            raise SandboxUnavailable(
                "process-group kill (killpg) is unavailable on this host — "
                "refusing to execute untrusted code"
            )

    # ------------------------------------------------------------------ #
    def run(
        self,
        argv: list[str],
        stdin_text: str = "",
        limits: ExecLimits | None = None,
        extra_env: dict[str, str] | None = None,
        writable_paths: list[str] | None = None,
    ) -> ExecutionResult:
        """Execute ``argv`` in the sandbox and return a structured result.

        ``writable_paths`` grants additional Landlock write access outside the
        run dir (used by the compiler for its output directory).
        """
        self.ensure_available()
        limits = limits or ExecLimits()
        run_dir = Path(tempfile.mkdtemp(prefix="ag_run_", dir=str(self.base_dir)))
        try:
            return self._execute(
                list(map(str, argv)), stdin_text, limits, run_dir, extra_env or {},
                writable_paths or [],
            )
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def _execute(
        self,
        argv: list[str],
        stdin_text: str,
        limits: ExecLimits,
        run_dir: Path,
        extra_env: dict[str, str],
        writable_paths: list[str],
    ) -> ExecutionResult:
        warnings: list[str] = []
        status_r, status_w = os.pipe()
        preexec = _make_preexec(
            limits, str(run_dir), status_w, self.use_network_namespace, self.use_landlock,
            writable_paths,
        )
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        env.update(extra_env)
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(run_dir),
                env=env,
                start_new_session=True,
                preexec_fn=preexec,
                pass_fds=(status_w,),
            )
        except Exception as exc:
            os.close(status_r)
            os.close(status_w)
            raise SandboxError(f"sandbox could not start {argv[0]!r}: {exc}") from exc
        os.close(status_w)

        try:
            flags_b = os.read(status_r, 1)
        except OSError:
            flags_b = b""
        os.close(status_r)
        if not flags_b:
            # Child died in preexec before reporting (e.g. rlimit failure).
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            raise SandboxError("sandbox child failed during setup; untrusted code not executed")
        flags = flags_b[0]
        netns_ok = bool(flags & _F_NETNS)
        landlock_ok = bool(flags & _F_LANDLOCK)
        if self.use_landlock and not landlock_ok:
            # Child exited 125 before exec — reap it and refuse the run.
            try:
                os.wait4(proc.pid, 0)
            except ChildProcessError:
                pass
            proc.returncode = _EXIT_SETUP_FAILED
            for f in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    f.close()
                except Exception:
                    pass
            raise SandboxError(
                "Landlock filesystem containment could not be applied; "
                "untrusted code was NOT executed"
            )
        if self.use_network_namespace and not netns_ok:
            warnings.append("network namespace unavailable; program ran with host networking")

        out_b, err_b, timed_out, truncated, status, rusage = self._pump(
            proc, stdin_text.encode("utf-8", errors="replace"), limits
        )
        if timed_out:
            try:
                os.killpg(proc.pid, _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                _, status, rusage = os.wait4(proc.pid, 0)
            except ChildProcessError:
                status, rusage = None, None
            # Drain whatever the dying tree left in the pipes.
            out_b += _drain_nonblocking(proc.stdout)
            err_b += _drain_nonblocking(proc.stderr)
            # Re-apply the cap after the final drain.
            if len(out_b) > limits.output_bytes:
                out_b = out_b[: limits.output_bytes]
                truncated = True
            if len(err_b) > limits.output_bytes:
                err_b = err_b[: limits.output_bytes]
                truncated = True

        exit_code, sig = None, None
        if status is not None:
            if os.WIFEXITED(status):
                exit_code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                sig = os.WTERMSIG(status)
        # Keep Popen bookkeeping consistent with our manual wait4.
        proc.returncode = exit_code if exit_code is not None else (-sig if sig else None)
        for f in (proc.stdin, proc.stdout, proc.stderr):
            try:
                f.close()
            except Exception:
                pass

        wall_s = time.monotonic() - start
        cpu_s = (rusage.ru_utime + rusage.ru_stime) if rusage is not None else 0.0
        peak_kb = int(rusage.ru_maxrss) if rusage is not None else 0
        return ExecutionResult(
            argv=argv,
            stdout=out_b.decode("utf-8", errors="replace"),
            stderr=err_b.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            signal=sig,
            timed_out=timed_out,
            wall_s=wall_s,
            cpu_s=cpu_s,
            peak_mem_kb=peak_kb,
            output_truncated=truncated,
            enforcement=EnforcementReport(
                resource_limits=True,
                process_group_kill=True,
                network_isolated=netns_ok,
                filesystem_hardened=landlock_ok,
                temp_dir=str(run_dir),
            ),
            warnings=warnings,
        )

    # ------------------------------------------------------------------ #
    def _pump(self, proc, stdin_data: bytes, limits: ExecLimits):
        """Select-loop: feed stdin, cap stdout/stderr, enforce wall deadline.

        Returns (stdout, stderr, timed_out, truncated, wait4_status, rusage).
        """
        stdin_f, stdout_f, stderr_f = proc.stdin, proc.stdout, proc.stderr
        for f in (stdin_f, stdout_f, stderr_f):
            os.set_blocking(f.fileno(), False)
        out, err = bytearray(), bytearray()
        pending = stdin_data
        in_open = bool(pending)
        if not in_open:
            stdin_f.close()
        out_open = err_open = True
        truncated = False
        cap = limits.output_bytes
        deadline = time.monotonic() + limits.wall_time_s
        drain_deadline: float | None = None  # short grace to drain pipes after exit
        status, rusage, exited = None, None, False

        while True:
            if not exited:
                try:
                    pid, status, rusage = os.wait4(proc.pid, os.WNOHANG)
                    exited = pid == proc.pid
                except ChildProcessError:
                    exited = True  # reaped elsewhere; shouldn't happen
            if exited and drain_deadline is None:
                drain_deadline = time.monotonic() + 2.0
            rlist = []
            if out_open:
                rlist.append(stdout_f)
            if err_open:
                rlist.append(stderr_f)
            wlist = [stdin_f] if in_open else []
            if exited and not rlist and not wlist:
                break
            now = time.monotonic()
            effective_deadline = drain_deadline if exited else deadline
            if now >= effective_deadline:
                # Child still running -> timeout (caller killpgs). Child gone
                # but pipes not draining (orphaned grandchild) -> stop anyway.
                return bytes(out), bytes(err), not exited, truncated, status, rusage
            timeout = max(0.0, effective_deadline - now)
            try:
                rready, wready, _ = select.select(rlist, wlist, [], timeout if (rlist or wlist) else 0.05)
            except (OSError, ValueError):
                break
            for f in wready:
                try:
                    n = os.write(f.fileno(), pending)
                except (BrokenPipeError, OSError):
                    n = len(pending)
                pending = pending[n:]
                if not pending:
                    f.close()
                    in_open = False
            for f in rready:
                is_out = f is stdout_f
                try:
                    chunk = os.read(f.fileno(), 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    if is_out:
                        out_open = False
                    else:
                        err_open = False
                    continue
                buf = out if is_out else err
                room = cap - len(buf)
                if room <= 0:
                    truncated = True
                    continue  # drain-and-discard
                buf.extend(chunk[:room] if len(chunk) > room else chunk)
                if len(chunk) > room:
                    truncated = True
            if exited and not rlist and not wlist:
                break
        return bytes(out), bytes(err), False, truncated, status, rusage
