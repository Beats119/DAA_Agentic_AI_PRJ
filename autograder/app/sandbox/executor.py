"""Isolated execution of untrusted binaries — launcher-based, fail-closed.

Design (Phase B; see docs/SANDBOX.md):

- The parent NEVER uses ``preexec_fn``. Applying rlimits, namespaces and
  Landlock rules inside ``fork()`` of a multithreaded application (the
  coordinator and the API both run thread pools) is documented by CPython
  as unsafe. Instead, every run spawns a fresh single-threaded *launcher*
  process (``autograder.app.sandbox.launcher``) which applies the whole
  mandatory policy and then ``execve``s the target, reporting the ACTUAL
  enforcement back over a status pipe before it becomes the target.
- Mandatory policy (``sandbox/policy.py``): resource limits, Landlock
  filesystem allowlist (default-deny reads AND writes; only runtime
  libraries, the run directory, explicitly granted paths, and
  ``/dev/null``), network namespace, a restrictive seccomp allowlist, and
  ``no_new_privs``. If any mandatory step fails, the launcher exits 125
  before exec and the parent raises :class:`SandboxError` — the target
  never runs with weaker isolation.
- Identity: when the application runs as root, the child drops to the
  dedicated unprivileged ``sandbox_uid``/``sandbox_gid`` (default
  ``nobody``/``nogroup``) before exec. This also makes ``RLIMIT_NPROC``
  meaningful (it is not enforced for uid 0).
- Process containment: the launcher starts in its own session
  (``start_new_session=True``); on wall-clock timeout the parent kills
  the whole process group. stdout/stderr are pumped through a select
  loop with byte caps. Each run gets a fresh temp cwd, removed in a
  ``finally`` even on timeout/crash. The environment is sanitized
  (``PATH``, ``LANG``, plus caller extras).

:meth:`SandboxExecutor.ensure_available` fail-closes: it checks the
capability probes against the policy's mandatory list AND runs a
functional self-test (a real sandboxed ``cat`` round-trip through the
launcher). If the mandatory bar cannot be met, nothing executes.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal as _signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..core.schemas import StrictModel
from .capabilities import SandboxCapabilities, probe_capabilities
from .landlock import FS_EXECUTE, FS_READ_EXEC, FS_READ_FILE, FS_WRITE_FILE, fs_all_rights
from .policy import KNOWN_CAPABILITIES, SandboxPolicy, load_policy
from .seccomp import PROFILES

SANDBOX_VERSION = "sandbox-2.0"

#: Directories whose binaries are invoked in place (with a per-file
#: Landlock execute rule) instead of being copied into the run directory.
#: Copying a system tool would break its relative resource lookups
#: (gcc finds cc1 relative to its own path); copying a *student* binary
#: is always safe and avoids ancestor-traversal problems entirely.
_SYSTEM_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/", "/opt/")

#: Mandatory capability -> field in the launcher's status report.
_STATUS_FIELDS = {
    "resource_limits": "resource_limits",
    "landlock_fs": "landlock",
    "network_namespace": "netns",
    "seccomp_filter": "seccomp",
    "no_new_privs": "no_new_privs",
}


class SandboxError(Exception):
    """Something went wrong setting up or running the sandbox."""


class SandboxUnavailable(SandboxError):
    """The host cannot provide the mandatory isolation bar. Refusing to run."""


class ExecLimits(StrictModel):
    """Per-run limits. Applied via rlimits (launcher) and the pump (parent)."""

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
    sanitizer itself caps *physical* memory. Landlock, seccomp, netns and
    the identity drop still apply in full. Never enable for untrusted,
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
    """What was ACTUALLY enforced for one run, per the launcher's report.

    These are actuals read back from the process that applied them — not
    capability probes and not intentions.
    """

    resource_limits: bool = False
    process_group_kill: bool = False
    network_isolated: bool = False
    landlock_fs: bool = False
    seccomp_filter: bool = False
    uid: int | None = None
    uid_dropped: bool = False
    # Legacy alias of landlock_fs (pre-Phase-B field name).
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
    """Runs argv-isolated binaries under the mandatory sandbox policy."""

    def __init__(
        self,
        base_dir: str | Path | None = None,
        use_network_namespace: bool = True,
        use_landlock: bool = True,
        policy: SandboxPolicy | None = None,
    ) -> None:
        self.base_dir = Path(base_dir or Path(tempfile.gettempdir()) / "autograder_sandbox")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.use_network_namespace = use_network_namespace
        self.use_landlock = use_landlock
        self.policy = policy or load_policy()
        self._caps = probe_capabilities()
        self._self_test_ok: bool | None = None

    @property
    def capabilities(self) -> SandboxCapabilities:
        return self._caps

    # ------------------------------------------------------------------ #
    def ensure_available(self, policy: SandboxPolicy | None = None) -> None:
        """Fail closed: refuse to run when the mandatory bar is not met.

        Checks the capability probes against ``policy.mandatory`` and then
        a functional self-test through the real launcher. Raises
        :class:`SandboxUnavailable` on any gap.
        """
        policy = policy or self.policy
        mandatory = list(policy.mandatory)
        unknown = sorted(set(mandatory) - KNOWN_CAPABILITIES)
        if unknown:
            raise SandboxUnavailable(
                f"sandbox policy mandates unknown capabilities {unknown}; "
                "refusing to guess at their enforcement"
            )
        caps = self._caps
        if "resource_limits" in mandatory and not caps.resource_limits:
            raise SandboxUnavailable(
                "resource limits (RLIMIT) are unavailable on this host — "
                "refusing to execute untrusted code"
            )
        if not hasattr(os, "killpg"):
            raise SandboxUnavailable(
                "process-group kill (killpg) is unavailable on this host — "
                "refusing to execute untrusted code"
            )
        if "landlock_fs" in mandatory:
            if not self.use_landlock:
                raise SandboxUnavailable(
                    "policy mandates Landlock filesystem isolation but this "
                    "executor was configured without it"
                )
            if caps.landlock_abi is None:
                raise SandboxUnavailable(
                    "Landlock is unavailable on this host — refusing to "
                    "execute untrusted code without filesystem isolation"
                )
        if "network_namespace" in mandatory:
            if not self.use_network_namespace:
                raise SandboxUnavailable(
                    "policy mandates network-namespace isolation but this "
                    "executor was configured without it"
                )
            if not caps.network_namespace:
                raise SandboxUnavailable(
                    "network namespaces are unavailable on this host — "
                    "refusing to execute untrusted code with host networking"
                )
        if "seccomp_filter" in mandatory and not caps.seccomp:
            raise SandboxUnavailable(
                "a restrictive seccomp filter cannot be installed on this "
                "host — refusing to execute untrusted code unfiltered"
            )
        if not self._functional_self_test():
            raise SandboxUnavailable(
                "sandbox functional self-test failed (a trivial program "
                "could not run under the full mandatory policy) — refusing "
                "to execute untrusted code"
            )

    def _functional_self_test(self) -> bool:
        """Run one trivial program through the REAL launcher, once.

        Proves end-to-end that netns + rlimits + identity drop + Landlock
        + seccomp compose into a working sandbox on this host — probes
        alone do not prove that.
        """
        if self._self_test_ok is not None:
            return self._self_test_ok
        ok = False
        try:
            cat = shutil.which("cat")
            limits = ExecLimits(wall_time_s=15.0, cpu_time_s=15.0)
            if cat:
                res = self._execute([cat], "autograder-sandbox-self-test", limits, {}, [], [], None)
                ok = (
                    res.stdout == "autograder-sandbox-self-test"
                    and res.enforcement.landlock_fs
                    and res.enforcement.seccomp_filter
                    and res.enforcement.network_isolated
                )
            else:
                res = self._execute(["/usr/bin/true"], "", limits, {}, [], [], None)
                ok = res.exit_code == 0
        except SandboxError:
            ok = False
        self._self_test_ok = ok
        return ok

    # ------------------------------------------------------------------ #
    def run(
        self,
        argv: list[str],
        stdin_text: str = "",
        limits: ExecLimits | None = None,
        extra_env: dict[str, str] | None = None,
        writable_paths: list[str] | None = None,
        *,
        read_paths: list[str] | None = None,
        seccomp_profile: str | None = None,
    ) -> ExecutionResult:
        """Execute ``argv`` in the sandbox and return a structured result.

        ``writable_paths`` grants full Landlock access to extra directories
        (the compiler's output directory). ``read_paths`` grants read+exec
        (the compiler's source directory, system include/bin dirs).
        ``seccomp_profile`` overrides the policy's default profile for
        this run (``"compiler"`` for compilation, ``"runtime"`` otherwise).
        """
        self.ensure_available()
        return self._execute(
            list(map(str, argv)),
            stdin_text,
            limits or ExecLimits(),
            extra_env or {},
            writable_paths or [],
            read_paths or [],
            seccomp_profile,
        )

    # ------------------------------------------------------------------ #
    def _execute(
        self,
        argv: list[str],
        stdin_text: str,
        limits: ExecLimits,
        extra_env: dict[str, str],
        writable_paths: list[str],
        read_paths: list[str],
        seccomp_profile: str | None,
    ) -> ExecutionResult:
        policy = self.policy
        profile = seccomp_profile or policy.seccomp_profile
        if profile not in PROFILES:
            raise SandboxError(f"unknown seccomp profile: {profile!r}")
        run_dir = Path(tempfile.mkdtemp(prefix="ag_run_", dir=str(self.base_dir)))
        spec_path: Path | None = None
        try:
            env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
            env.update(extra_env)
            argv_final, argv_file_rule = self._prepare_argv(argv, run_dir, env["PATH"])
            rules = self._build_rules(run_dir, argv_file_rule, writable_paths, read_paths)
            self._prepare_dac(run_dir, writable_paths, read_paths)

            status_r, status_w = os.pipe()
            spec = {
                "argv": argv_final,
                "cwd": str(run_dir),
                "env": env,
                "limits": limits.model_dump(),
                "policy": {
                    "mandatory": list(policy.mandatory),
                    "sandbox_uid": policy.sandbox_uid,
                    "sandbox_gid": policy.sandbox_gid,
                    "drop_privileges_when_root": policy.drop_privileges_when_root,
                    "seccomp_profile": profile,
                },
                "landlock_rules": [[p, r] for p, r in rules],
                "status_fd": status_w,
                "want_netns": self.use_network_namespace,
                "want_landlock": self.use_landlock,
            }
            fd, spec_name = tempfile.mkstemp(prefix="ag_spec_", suffix=".json", dir=str(self.base_dir))
            spec_path = Path(spec_name)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(spec, fh)

            launcher_env = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                # The launcher must import the autograder package no matter
                # what cwd/environment the target runs with.
                "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
            }
            start = time.monotonic()
            try:
                # NOTE: no preexec_fn — see the module docstring. The child
                # is the launcher process; it applies the policy itself in
                # a single-threaded interpreter and execve()s the target.
                proc = subprocess.Popen(
                    [sys.executable, "-m", "autograder.app.sandbox.launcher", str(spec_path)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(run_dir),
                    env=launcher_env,
                    start_new_session=True,
                    pass_fds=(status_w,),
                )
            except Exception as exc:
                os.close(status_r)
                os.close(status_w)
                raise SandboxError(f"sandbox could not start the launcher: {exc}") from exc
            os.close(status_w)

            try:
                status = self._read_status(status_r, proc)
            finally:
                os.close(status_r)
            self._check_status(status, proc)

            out_b, err_b, timed_out, truncated, wstatus, rusage = self._pump(
                proc, stdin_text.encode("utf-8", errors="replace"), limits
            )
            if timed_out:
                try:
                    os.killpg(proc.pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    _, wstatus, rusage = os.wait4(proc.pid, 0)
                except ChildProcessError:
                    wstatus, rusage = None, None
                out_b += _drain_nonblocking(proc.stdout)
                err_b += _drain_nonblocking(proc.stderr)
                if len(out_b) > limits.output_bytes:
                    out_b = out_b[: limits.output_bytes]
                    truncated = True
                if len(err_b) > limits.output_bytes:
                    err_b = err_b[: limits.output_bytes]
                    truncated = True

            exit_code, sig = None, None
            if wstatus is not None:
                if os.WIFEXITED(wstatus):
                    exit_code = os.WEXITSTATUS(wstatus)
                elif os.WIFSIGNALED(wstatus):
                    sig = os.WTERMSIG(wstatus)
            proc.returncode = exit_code if exit_code is not None else (-sig if sig else None)
            for f in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    f.close()
                except Exception:
                    pass

            warnings: list[str] = []
            if not status["uid_dropped"]:
                warnings.append(
                    "identity drop not applied: "
                    + str(status.get("uid_drop_reason") or "unknown reason")
                )
            return ExecutionResult(
                argv=argv,
                stdout=out_b.decode("utf-8", errors="replace"),
                stderr=err_b.decode("utf-8", errors="replace"),
                exit_code=exit_code,
                signal=sig,
                timed_out=timed_out,
                wall_s=time.monotonic() - start,
                cpu_s=(rusage.ru_utime + rusage.ru_stime) if rusage is not None else 0.0,
                peak_mem_kb=int(rusage.ru_maxrss) if rusage is not None else 0,
                output_truncated=truncated,
                enforcement=EnforcementReport(
                    resource_limits=bool(status["resource_limits"]),
                    process_group_kill=True,
                    network_isolated=bool(status["netns"]),
                    landlock_fs=bool(status["landlock"]),
                    seccomp_filter=bool(status["seccomp"]),
                    uid=int(status["uid"]),
                    uid_dropped=bool(status["uid_dropped"]),
                    filesystem_hardened=bool(status["landlock"]),
                    temp_dir=str(run_dir),
                ),
                warnings=warnings,
            )
        finally:
            if spec_path is not None:
                try:
                    spec_path.unlink()
                except OSError:
                    pass
            shutil.rmtree(run_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def _prepare_argv(
        self, argv: list[str], run_dir: Path, path_env: str
    ) -> tuple[list[str], tuple[str, int] | None]:
        """Resolve argv[0]; copy non-system binaries into the run dir.

        Returns (final argv, optional per-file Landlock rule for argv[0]).
        The copy is invoked as ``./prog`` so execve resolves it through
        the (already-entered) run directory rather than a host path.
        """
        if not argv:
            raise SandboxError("empty argv")
        raw = argv[0]
        if "/" in raw:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = run_dir / raw
            if not candidate.is_file():
                raise SandboxError(f"executable not found: {raw!r}")
            resolved = candidate.resolve()
        else:
            found = shutil.which(raw, path=path_env)
            if not found:
                raise SandboxError(f"executable {raw!r} not found in PATH ({path_env})")
            resolved = Path(found).resolve()
        if str(resolved).startswith(_SYSTEM_PREFIXES):
            return [str(resolved), *argv[1:]], (str(resolved), FS_READ_FILE | FS_EXECUTE)
        dest = run_dir / "prog"
        shutil.copyfile(resolved, dest)
        dest.chmod(0o755)
        if os.geteuid() == 0 and self.policy.drop_privileges_when_root:
            os.chown(dest, self.policy.sandbox_uid, self.policy.sandbox_gid)
        return ["./prog", *argv[1:]], None

    def _build_rules(
        self,
        run_dir: Path,
        argv_file_rule: tuple[str, int] | None,
        writable_paths: list[str],
        read_paths: list[str],
    ) -> list[tuple[str, int]]:
        """The complete Landlock allowlist for one run. Default is deny."""
        merged: dict[str, int] = {}

        def grant(path: str, rights: int) -> None:
            merged[path] = merged.get(path, 0) | rights

        grant(str(run_dir), fs_all_rights())
        for p in self.policy.fs_read_paths:
            if os.path.isdir(p):
                grant(p, FS_READ_EXEC)
        grant("/dev/null", FS_READ_FILE | FS_WRITE_FILE)
        for dev in ("/dev/zero", "/dev/urandom"):
            if os.path.exists(dev):
                grant(dev, FS_READ_FILE)
        for p in writable_paths:
            grant(str(p), fs_all_rights())
        for p in read_paths:
            if os.path.exists(p):
                grant(str(p), FS_READ_EXEC)
        if argv_file_rule is not None:
            grant(argv_file_rule[0], argv_file_rule[1])
        return sorted(merged.items())

    # ------------------------------------------------------------------ #
    def _prepare_dac(self, run_dir: Path, writable_paths: list[str], read_paths: list[str]) -> None:
        """Make granted paths usable by the sandbox uid (DAC side).

        Landlock is an allowlist *on top of* DAC: both must permit. When
        the child will run as ``sandbox_uid`` (we are root and will drop),
        the run directory and compiler output directories are chowned to
        it, and ancestor directories of granted paths get traverse-only
        (o+x) permission where the sandbox identity could not otherwise
        pass. No read/write bits are ever added to ancestors, and nothing
        is changed when not dropping privileges.
        """
        if os.geteuid() != 0 or not self.policy.drop_privileges_when_root:
            return
        uid, gid = self.policy.sandbox_uid, self.policy.sandbox_gid
        os.chown(run_dir, uid, gid)
        self._ensure_traversable(run_dir, uid, gid)
        for p in writable_paths:
            path = Path(p)
            self._ensure_traversable(path, uid, gid)
            if path.is_dir():
                for root, dirs, files in os.walk(path):
                    os.chown(root, uid, gid)
                    for name in dirs + files:
                        try:
                            os.chown(os.path.join(root, name), uid, gid)
                        except OSError:
                            pass
        for p in read_paths:
            path = Path(p)
            self._ensure_traversable(path, uid, gid)
            self._ensure_readable(path, uid, gid)

    @staticmethod
    def _ensure_readable(path: Path, uid: int, gid: int) -> None:
        """Ensure the sandbox identity can READ a granted read path.

        For a file: o+r when it is not otherwise readable by the sandbox
        identity. For a directory: the same for its direct child files
        (e.g. the source file in a granted source directory). Only paths
        the caller explicitly granted are touched.
        """
        def _fix(fp: Path) -> None:
            try:
                st = fp.stat()
            except OSError:
                return
            readable = (
                bool(st.st_mode & 0o004)
                or (st.st_uid == uid and bool(st.st_mode & 0o400))
                or (st.st_gid == gid and bool(st.st_mode & 0o040))
            )
            if not readable:
                try:
                    os.chmod(fp, st.st_mode | 0o004)
                except OSError:
                    pass

        if path.is_file():
            _fix(path)
        elif path.is_dir():
            try:
                for child in path.iterdir():
                    if child.is_file():
                        _fix(child)
            except OSError:
                pass

    @staticmethod
    def _ensure_traversable(path: Path, uid: int, gid: int) -> None:
        """Add o+x to any ancestor the sandbox identity cannot traverse."""
        for anc in [path, *path.parents]:
            try:
                st = anc.stat()
            except OSError:
                continue
            traversable = (
                bool(st.st_mode & 0o001)
                or (st.st_uid == uid and bool(st.st_mode & 0o100))
                or (st.st_gid == gid and bool(st.st_mode & 0o010))
            )
            if not traversable:
                try:
                    os.chmod(anc, st.st_mode | 0o001)
                except OSError:
                    pass

    # ------------------------------------------------------------------ #
    def _read_status(self, status_r: int, proc: subprocess.Popen) -> dict:
        """Read the launcher's one-line JSON status (bounded wait)."""
        buf = bytearray()
        deadline = time.monotonic() + 30.0
        os.set_blocking(status_r, False)
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_tree(proc)
                raise SandboxError("sandbox launcher did not report status in time; run refused")
            ready, _, _ = select.select([status_r], [], [], min(remaining, 0.5))
            if not ready:
                if proc.poll() is not None:
                    break  # launcher died; whatever we have is all there is
                continue
            chunk = os.read(status_r, 4096)
            if not chunk:
                break  # EOF
            buf.extend(chunk)
        line = bytes(buf).split(b"\n", 1)[0].strip()
        if not line:
            rc = proc.poll()
            raise SandboxError(
                f"sandbox launcher failed during setup (exit {rc}); untrusted code not executed"
            )
        try:
            return json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            self._kill_tree(proc)
            raise SandboxError(f"sandbox launcher sent an invalid status line: {exc}") from exc

    def _check_status(self, status: dict, proc: subprocess.Popen) -> None:
        """Refuse the run unless the launcher reports the mandatory bar."""
        if not status.get("ok"):
            self._kill_tree(proc)
            raise SandboxError(
                "sandbox refused to run: mandatory step "
                f"{status.get('failed_step')!r} failed: {status.get('error')}"
            )
        for cap in self.policy.mandatory:
            field = _STATUS_FIELDS.get(cap)
            if field is not None and not status.get(field):
                self._kill_tree(proc)
                raise SandboxError(
                    f"sandbox refused to run: policy mandates {cap!r} but the "
                    "launcher did not establish it"
                )

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, _signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

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
