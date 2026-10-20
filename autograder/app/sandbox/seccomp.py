"""Restrictive seccomp-BPF syscall allowlists for sandboxed processes.

Implemented directly on classic BPF via ctypes (no libseccomp dependency).
The filter validates the architecture, compares the syscall number against
a per-profile allowlist, and returns ``EPERM`` for everything else. This
is *enforcement*, not a probe: :func:`install_filter` either installs the
filter in the calling process (irreversibly) or raises.

Profiles
--------
``runtime`` — what a normal dynamically-linked C program needs: stdio,
memory management, signals, time, identity queries, process creation
(see the note below), and exec of an already-approved binary. Deliberately
ABSENT: the whole socket family (``socket``/``connect``/``bind``/
``accept``/…), ``ptrace``, ``mount``/``umount``, ``unshare``, the
``setuid``/``setgid`` family, ``init_module``, ``bpf``, ``perf_event_open``,
``keyctl``, ``io_uring_setup`` and everything not listed — the default
action denies with ``EPERM``.

Process creation (``fork``/``vfork``/``clone``/``clone3``/``wait4``) IS in
the runtime profile, on purpose: legitimate C programs fork, and the
containment for process creation comes from ``RLIMIT_NPROC`` (effective
because the child runs as the unprivileged sandbox uid, not root), the
wall/CPU limits, and the executor's process-group kill — all verified by
``tests/security/test_forkbomb.py`` and ``test_timeout.py``, which require
a real forked child to exist and then prove the whole tree dies. Denying
``fork`` would not add containment; it would only make those proofs
vacuous and break ordinary programs.

``compiler`` — ``runtime`` plus what gcc/cc1/as/ld need: exec of helper
tools, file creation/renaming in the output directory, ``fcntl``,
``getrusage``, ``kill`` (gcc terminating its own children), etc.

``madvise``/``mremap``/``clock_nanosleep`` are in ``runtime`` although not
in the original profile sketch: glibc's malloc calls ``madvise`` on free
and its ``sleep()`` issues ``clock_nanosleep`` — without them ordinary
programs misbehave. Both are pure in-process operations. ``readlinkat``
is required by ASan's symbolizer. ``symlink``/``symlinkat`` are allowed
because creating a link is confined by Landlock: the link TARGET is
evaluated against the allowlist when it is followed, so links cannot
smuggle a program out of its sandbox (proven by
``tests/security/test_symlink_escape.py``).

Only x86_64 is supported: syscall numbers are architecture-specific, and
installing a wrong-architecture table would be worse than refusing. On
any other architecture :func:`install_filter` raises
:class:`SeccompUnavailable` and the caller must refuse to run.
"""

from __future__ import annotations

import ctypes
import os
import platform
import struct

# --- seccomp / BPF constants -------------------------------------------------
SECCOMP_MODE_FILTER = 2
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
EPERM = 1
AUDIT_ARCH_X86_64 = 0xC000003E

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22

# classic BPF instruction classes
_BPF_LD_W_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_BPF_JMP_JEQ_K = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_BPF_RET_K = 0x06  # BPF_RET | BPF_K

_OFF_NR = 0  # offsetof(struct seccomp_data, nr)
_OFF_ARCH = 4  # offsetof(struct seccomp_data, arch)

# --- x86_64 syscall numbers ---------------------------------------------------
# Reference: arch/x86/entry/syscalls/syscall_64.tbl. Only the numbers used
# below are listed; the allowlists are built from NAMES so a typo'd number
# cannot silently allow the wrong syscall under a familiar name.
_NR = {
    "read": 0, "write": 1, "open": 2, "close": 3, "stat": 4, "fstat": 5,
    "poll": 7, "lseek": 8, "mmap": 9, "mprotect": 10, "munmap": 11,
    "brk": 12, "rt_sigaction": 13, "rt_sigprocmask": 14, "rt_sigreturn": 15,
    "ioctl": 16, "pread64": 17, "pwrite64": 18, "readv": 19, "writev": 20,
    "access": 21, "pipe": 22, "select": 23, "sched_yield": 24, "mremap": 25,
    "madvise": 28, "dup": 32, "dup2": 33, "nanosleep": 35, "alarm": 37,
    "getpid": 39, "sendfile": 40, "clone": 56, "fork": 57, "vfork": 58,
    "execve": 59, "exit": 60, "wait4": 61, "kill": 62, "uname": 63,
    "fcntl": 72, "truncate": 76, "ftruncate": 77, "getcwd": 79, "chdir": 80,
    "rename": 82, "mkdir": 83, "link": 86, "unlink": 87, "symlink": 88,
    "readlink": 89, "chmod": 90, "umask": 95, "gettimeofday": 96,
    "getrlimit": 97, "getrusage": 98, "sysinfo": 99, "times": 100,
    "getuid": 102, "getgid": 104, "geteuid": 107, "getegid": 108,
    "getppid": 110, "sigaltstack": 131, "setpgid": 154, "arch_prctl": 158,
    "gettid": 186, "futex": 202, "sched_getaffinity": 204,
    "set_tid_address": 218, "getdents64": 217, "clock_gettime": 228,
    "clock_nanosleep": 230, "exit_group": 231, "waitid": 247,
    "openat": 257, "newfstatat": 262, "unlinkat": 263, "symlinkat": 266,
    "readlinkat": 267, "faccessat": 269,
    "pselect6": 270, "ppoll": 271, "set_robust_list": 273, "utimensat": 280,
    "fallocate": 285, "pipe2": 293, "dup3": 292, "prlimit64": 302,
    "getrandom": 318, "execveat": 322, "copy_file_range": 326, "statx": 332,
    "rseq": 334, "clone3": 435,
}

_RUNTIME_NAMES = [
    # I/O and file access (paths themselves are governed by Landlock)
    "read", "write", "open", "openat", "close", "fstat", "newfstatat",
    "stat", "statx", "lseek", "pread64", "pwrite64", "readv", "writev",
    "access", "faccessat", "readlink", "readlinkat", "getdents64", "getcwd", "umask",
    "symlink", "symlinkat",  # link creation only; Landlock judges the target at follow time
    "fcntl",  # fcntl: F_GETFL/F_SETFL on own fds; harmless, needed by stdio users
    # memory
    "mmap", "mprotect", "munmap", "brk", "mremap", "madvise",
    # process lifecycle (containment: RLIMIT_NPROC + killpg — see docstring)
    "fork", "vfork", "clone", "clone3", "wait4", "waitid", "execve",
    "exit", "exit_group", "getpid", "gettid", "getppid",
    # signals
    "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "sigaltstack",
    # identity (queries only — the setuid family is NOT included)
    "getuid", "geteuid", "getgid", "getegid", "uname", "sysinfo", "times",
    # time
    "clock_gettime", "clock_nanosleep", "nanosleep",
    # synchronisation / misc runtime
    "futex", "sched_getaffinity", "sched_yield", "getrandom", "ioctl",
    "pipe", "pipe2", "dup", "dup2", "dup3",
    "poll", "ppoll", "select", "pselect6",
    # loader / glibc startup
    "arch_prctl", "set_tid_address", "set_robust_list", "rseq", "prlimit64",
]

_COMPILER_EXTRA_NAMES = [
    # gcc/cc1/as/ld process management and file production
    "execveat", "chdir", "mkdir", "unlink", "unlinkat", "rename", "link",
    "symlink", "chmod", "utimensat", "truncate", "ftruncate", "fallocate",
    "copy_file_range", "sendfile", "kill", "setpgid", "alarm",
    "getrusage", "gettimeofday", "getrlimit",
]

PROFILES: dict[str, frozenset[int]] = {
    "runtime": frozenset(_NR[n] for n in _RUNTIME_NAMES),
    "compiler": frozenset(_NR[n] for n in _RUNTIME_NAMES + _COMPILER_EXTRA_NAMES),
}


class SeccompUnavailable(RuntimeError):
    """A restrictive seccomp filter cannot be installed on this host/arch."""


def _insn(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def build_filter(profile: str) -> bytes:
    """Build the classic-BPF program bytes for ``profile``.

    Layout: check arch (kill on mismatch) → load syscall nr → one JEQ per
    allowed number jumping to RET_ALLOW → fall through to RET_ERRNO(EPERM)
    → RET_ALLOW. Jump offsets are 8-bit; the profiles are far below the
    255 limit, which is asserted.
    """
    try:
        allowed = sorted(PROFILES[profile])
    except KeyError:
        raise SeccompUnavailable(f"unknown seccomp profile: {profile!r}") from None
    n = len(allowed)
    assert n + 2 <= 255, "allowlist too large for classic-BPF 8-bit jumps"
    insns = [
        _insn(_BPF_LD_W_ABS, 0, 0, _OFF_ARCH),
        _insn(_BPF_JMP_JEQ_K, 1, 0, AUDIT_ARCH_X86_64),  # match → skip KILL
        _insn(_BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),
        _insn(_BPF_LD_W_ABS, 0, 0, _OFF_NR),
    ]
    # JEQ at position i (0-based within the checks) must jump to the
    # RET_ALLOW at index 4 + n + 1: jt = (4 + n + 1) - (4 + i) - 1 = n - i.
    for i, nr in enumerate(allowed):
        insns.append(_insn(_BPF_JMP_JEQ_K, n - i, 0, nr))
    insns.append(_insn(_BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | EPERM))
    insns.append(_insn(_BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW))
    return b"".join(insns)


def install_filter(profile: str) -> None:
    """Install the ``profile`` seccomp filter in the CALLING process.

    Irreversible. Raises :class:`SeccompUnavailable` on any failure — the
    caller (the sandbox launcher) treats that as a refused run whenever
    the profile is mandatory.
    """
    if platform.machine() != "x86_64":
        raise SeccompUnavailable(
            f"seccomp profiles are defined for x86_64 only; this host is "
            f"{platform.machine()!r}"
        )
    prog = build_filter(profile)
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    # A filter can only be installed with no_new_privs set (or CAP_SYS_ADMIN).
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SeccompUnavailable(
            f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(ctypes.get_errno())}"
        )
    buf = ctypes.create_string_buffer(prog, len(prog))
    # struct sock_fprog { unsigned short len; struct sock_filter *filter; }
    fprog = struct.pack("HQ", len(prog) // 8, ctypes.addressof(buf))
    if libc.prctl(_PR_SET_SECCOMP, SECCOMP_MODE_FILTER, fprog) != 0:
        raise SeccompUnavailable(
            f"prctl(PR_SET_SECCOMP, {profile}) failed: "
            f"{os.strerror(ctypes.get_errno())}"
        )
