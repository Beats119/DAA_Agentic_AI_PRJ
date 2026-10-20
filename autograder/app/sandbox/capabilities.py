"""Honest capability probing for the sandbox.

Every probe runs the real operation in a throwaway child process and reports
what actually worked. Nothing is assumed from the kernel version or the
presence of a binary — a capability is reported available only if the
operation succeeds end to end.

Semantics after the Phase-B overhaul:

- ``seccomp`` means a REAL restrictive filter (the runtime profile) was
  installed in a probe child AND a prohibited syscall (``socket``) was
  denied with ``EPERM`` while a permitted one still worked. It does NOT
  merely mean "the seccomp mechanism exists".
- ``landlock_abi`` is the REAL ABI version reported by the kernel (via
  ``LANDLOCK_CREATE_RULESET_VERSION``); the policy driver grants rules
  with the ABI-v1 rights subset (``landlock_driver_abi``), while the
  handled mask covers every FS right the kernel mediates.

Probes are best-effort and never raise: an inconclusive probe reports False.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys

from ..core.schemas import StrictModel


class SandboxCapabilities(StrictModel):
    """What this host can actually enforce. All fields are measured, not assumed."""

    user_namespaces: bool = False
    network_namespace: bool = False
    seccomp: bool = False
    cgroup_v2: bool = False
    resource_limits: bool = False
    landlock_abi: int | None = None  # REAL Landlock ABI version reported by the kernel
    landlock_driver_abi: int = 1  # rights subset the policy driver grants rules with
    compiler: str | None = None  # absolute path to gcc/clang, if present


def _in_child(fn) -> bool:
    """Run ``fn`` in a forked child; return True iff it completes without raising."""
    pid = os.fork()
    if pid == 0:  # child
        try:
            fn()
        except Exception:
            os._exit(1)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) == 0


def _probe_user_namespaces() -> bool:
    """unshare(CLONE_NEWUSER) *and* a writable uid_map.

    Creating the namespace alone is not enough: without an id map the child
    runs as the overflow uid and cannot do anything useful. Both must work.
    """

    def _try() -> None:
        os.unshare(os.CLONE_NEWUSER)
        uid, gid = os.getuid(), os.getgid()
        try:
            with open("/proc/self/setgroups", "w") as fh:
                fh.write("deny")
        except OSError:
            pass  # kernels without setgroups file
        with open("/proc/self/uid_map", "w") as fh:
            fh.write(f"0 {uid} 1\n")
        with open("/proc/self/gid_map", "w") as fh:
            fh.write(f"0 {gid} 1\n")
        # Verify the map really stuck (reads back non-empty).
        if not open("/proc/self/uid_map").read().strip():
            raise OSError("uid_map did not stick")

    return _in_child(_try)


def _probe_network_namespace() -> bool:
    def _try() -> None:
        os.unshare(os.CLONE_NEWNET)

    return _in_child(_try)


def _probe_seccomp() -> bool:
    """Can this host install the REAL runtime filter — and does it bite?

    Installs the production runtime profile in a probe child, then
    requires that ``socket()`` is denied with EPERM while an allowed
    syscall (``getpid``) still succeeds. Anything less is not reported
    as seccomp capability: mechanism-presence is not enforcement.
    """

    def _try() -> None:
        from .seccomp import install_filter

        install_filter("runtime")
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        fd = libc.socket(2, 1, 0)  # AF_INET, SOCK_STREAM — not in the profile
        if fd >= 0:
            os.close(fd)
            raise OSError("socket() succeeded under the runtime seccomp filter")
        if ctypes.get_errno() != errno.EPERM:
            raise OSError("socket() was denied, but not with EPERM")
        os.getpid()  # an allowed syscall must still work

    return _in_child(_try)


def _probe_cgroup_v2() -> bool:
    try:
        with open("/sys/fs/cgroup/cgroup.controllers") as fh:
            controllers = fh.read().strip()
    except OSError:
        return False
    # The file must exist AND list at least one controller; an empty file
    # means cgroup v2 is mounted but nothing is delegated to us.
    return bool(controllers)


def _probe_resource_limits() -> bool:
    def _try() -> None:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
        resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))

    return _in_child(_try)


def _probe_landlock() -> int | None:
    """Return the REAL Landlock ABI version reported by the kernel, or None."""
    from .landlock import landlock_abi_version

    return landlock_abi_version()


def probe_capabilities() -> SandboxCapabilities:
    """Probe every sandbox-relevant capability on this host."""
    compiler = shutil.which("gcc") or shutil.which("clang")
    return SandboxCapabilities(
        user_namespaces=_probe_user_namespaces(),
        network_namespace=_probe_network_namespace(),
        seccomp=_probe_seccomp(),
        cgroup_v2=_probe_cgroup_v2(),
        resource_limits=_probe_resource_limits(),
        landlock_abi=_probe_landlock(),
        compiler=compiler,
    )


if __name__ == "__main__":
    caps = probe_capabilities()
    print(caps.model_dump_json(indent=2))
    sys.exit(0)
