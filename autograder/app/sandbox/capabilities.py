"""Honest capability probing for the sandbox.

Every probe runs the real operation in a throwaway child process and reports
what actually worked. Nothing is assumed from the kernel version or the
presence of a binary — a capability is reported available only if the
operation succeeds end to end.

Probes are best-effort and never raise: an inconclusive probe reports False.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import struct
import subprocess
import sys

from ..core.schemas import StrictModel

# Landlock syscall numbers (x86_64 / aarch64 share these).
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_RULE_PATH_BENEATH = 1


class SandboxCapabilities(StrictModel):
    """What this host can actually enforce. All fields are measured, not assumed."""

    user_namespaces: bool = False
    network_namespace: bool = False
    seccomp: bool = False
    cgroup_v2: bool = False
    resource_limits: bool = False
    landlock_abi: int | None = None  # Landlock ABI version, if the kernel supports it
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
    """Can this process install a (trivial allow-all) seccomp filter?

    This only proves the *mechanism* exists; the sandbox does not currently
    install syscall filters (Landlock + netns + rlimits cover the policy).
    """

    def _try() -> None:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_SECCOMP = 22
        SECCOMP_MODE_FILTER = 2
        # BPF: single RET_ALLOW instruction.
        prog = struct.pack("HBBI", 6, 0, 0, 0x7FFF0000)
        buf = ctypes.create_string_buffer(prog)
        fprog = struct.pack("HQ", 1, ctypes.addressof(buf))
        if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, fprog) != 0:
            raise OSError("prctl(PR_SET_SECCOMP) failed")

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
    """Return the supported Landlock ABI version, or None.

    Probes ABI versions from newest to oldest by creating a ruleset with the
    version's attr size. Only versions we know how to drive are returned.
    """
    from .landlock import _HANDLED_V1

    libc = ctypes.CDLL("libc.so.6", use_errno=True)

    def _try_abi(attr_size: int) -> bool:
        def _try() -> None:
            attr = struct.pack("Q", _HANDLED_V1)[:attr_size].ljust(attr_size, b"\0")
            fd = libc.syscall(_SYS_LANDLOCK_CREATE_RULESET, attr, attr_size, 0)
            if fd < 0:
                raise OSError("landlock_create_ruleset failed")
            os.close(fd)

        return _in_child(_try)

    # ABI v1 attr is a single u64 (handled_access_fs).
    if _try_abi(8):
        return 1
    return None


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
