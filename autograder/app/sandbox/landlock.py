"""Landlock filesystem containment for unprivileged processes.

Implemented with ctypes against the raw syscalls (no libseccomp / no
privilege needed). Applied by the sandbox *launcher* in a fresh process,
immediately before ``execve`` — once restricted, the process cannot lift
the restriction, and neither can its children.

Policy (default-deny): the ruleset handles **all** filesystem access
rights the kernel offers (detected at runtime), and grants are an
allowlist built by the executor from the sandbox policy:

- the per-run directory: all rights (the only fully writable area);
- policy ``fs_read_paths`` (runtime libraries + loader ONLY — ``/lib``,
  ``/lib64``, ``/usr/lib``, ``/usr/lib64`` by default): read + execute;
- explicitly granted extra paths (compiler output dir, source dir, the
  invoked binary itself, the process's own ``/proc/self``);
- ``/dev/null``: read + write.

Everything else is denied — reads included. A sandboxed program cannot
read ``/etc/shadow``, SSH keys, ``.env`` files, other runs' artifacts, or
any path not on the allowlist, and cannot write anywhere outside its run
directory (plus explicitly granted compiler outputs).

The kernel on the reference VM reports Landlock ABI v8; the probe returns
the REAL ABI (via ``LANDLOCK_CREATE_RULESET_VERSION``). The driver grants
rules using the ABI-v1 rights subset for read paths (sufficient there),
while the handled mask covers every FS right the kernel knows — so newer
rights (REFER, TRUNCATE, IOCTL_DEV, …) are also default-denied outside
the run directory, and fully granted inside it.

Raises :class:`LandlockUnavailable` if the kernel does not support
Landlock, and :exc:`OSError` if any step of applying the policy fails
(fail closed: the caller must not exec the untrusted binary in that case).
"""

from __future__ import annotations

import ctypes
import functools
import os
import struct

# --- syscall numbers (x86_64 and aarch64) -----------------------------------
_SYS_CREATE_RULESET = 444
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446

_LANDLOCK_CREATE_RULESET_VERSION = 1
_PR_SET_NO_NEW_PRIVS = 38
_RULE_PATH_BENEATH = 1

# --- ABI v1 filesystem access rights -----------------------------------------
FS_EXECUTE = 0x1
FS_WRITE_FILE = 0x2
FS_READ_FILE = 0x4
FS_READ_DIR = 0x8
FS_REMOVE_DIR = 0x10
FS_REMOVE_FILE = 0x20
FS_MAKE_CHAR = 0x40
FS_MAKE_DIR = 0x80
FS_MAKE_REG = 0x100
FS_MAKE_SOCK = 0x200
FS_MAKE_FIFO = 0x400
FS_MAKE_BLOCK = 0x800
FS_MAKE_SYM = 0x1000
FS_REFER = 0x2000
FS_TRUNCATE = 0x4000
FS_IOCTL_DEV = 0x8000

FS_RW_DIR = (
    FS_READ_FILE
    | FS_WRITE_FILE
    | FS_READ_DIR
    | FS_REMOVE_DIR
    | FS_REMOVE_FILE
    | FS_MAKE_CHAR
    | FS_MAKE_DIR
    | FS_MAKE_REG
    | FS_MAKE_SOCK
    | FS_MAKE_FIFO
    | FS_MAKE_BLOCK
    | FS_MAKE_SYM
    | FS_REFER
    | FS_EXECUTE
)
FS_READ_EXEC = FS_READ_FILE | FS_READ_DIR | FS_EXECUTE

# Candidate handled-access masks, widest first. Kernels reject masks with
# bits they do not know (EINVAL), so the first mask the kernel accepts is
# exactly the set of FS rights it can mediate. 0x1FFF is the ABI-v1 set.
_HANDLED_CANDIDATES = (0x1FFFF, 0xFFFF, 0x7FFF, 0x3FFF, 0x1FFF)


class LandlockUnavailable(RuntimeError):
    """The running kernel does not support Landlock."""


def _libc():
    return ctypes.CDLL("libc.so.6", use_errno=True)


@functools.lru_cache(maxsize=1)
def landlock_abi_version() -> int | None:
    """Return the REAL supported Landlock ABI version, or None.

    Uses ``landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)``
    which returns the ABI version directly (8 on the reference kernel).
    Fork-free by design: this probe is a bare syscall and is safe to call
    from any context, including after rlimits are set.
    """
    libc = _libc()
    version = libc.syscall(_SYS_CREATE_RULESET, None, 0, _LANDLOCK_CREATE_RULESET_VERSION)
    if version is not None and version > 0:
        return int(version)
    # Fallback for kernels without the VERSION flag: a v1 ruleset works.
    attr = struct.pack("Q", 0x1FFF)
    fd = libc.syscall(_SYS_CREATE_RULESET, attr, 8, 0)
    if fd < 0:
        return None
    os.close(fd)
    return 1


@functools.lru_cache(maxsize=1)
def handled_fs_mask() -> int:
    """All FS access rights this kernel can mediate (default-deny scope)."""
    libc = _libc()
    for mask in _HANDLED_CANDIDATES:
        attr = struct.pack("Q", mask)
        fd = libc.syscall(_SYS_CREATE_RULESET, attr, 8, 0)
        if fd >= 0:
            os.close(fd)
            return mask
    raise LandlockUnavailable("kernel rejected every Landlock handled-access mask")


def fs_all_rights() -> int:
    """Every FS right the kernel mediates — granted on the run directory."""
    return handled_fs_mask()


def open_rule_fd(path: str) -> int:
    """Open an ``O_PATH`` fd for a rule target.

    Callers that drop privileges (the launcher) open every rule fd FIRST,
    while still privileged: fd-based rules are immune to later DAC changes,
    whereas opening the path after ``setuid`` would fail on any ancestor
    directory the sandbox uid cannot traverse.
    """
    return os.open(path, os.O_PATH | os.O_CLOEXEC)


def _add_fd_rule(libc, ruleset_fd: int, parent_fd: int, rights: int) -> None:
    # struct landlock_path_beneath_attr (packed): u64 allowed_access, s32 parent_fd
    attr = struct.pack("Qi", rights, parent_fd)
    rc = libc.syscall(_SYS_ADD_RULE, ruleset_fd, _RULE_PATH_BENEATH, attr, 0)
    if rc != 0:
        raise OSError(f"landlock_add_rule(fd={parent_fd}) failed: {os.strerror(ctypes.get_errno())}")


def _add_path_rule(libc, ruleset_fd: int, path: str, rights: int) -> None:
    fd = open_rule_fd(path)
    try:
        _add_fd_rule(libc, ruleset_fd, fd, rights)
    finally:
        os.close(fd)


def default_rules(run_dir: str) -> list[tuple[str, int]]:
    """The minimal filesystem policy for one run directory.

    Default-deny reads and writes: only the run directory itself (all
    rights), ``/dev/null`` (rw), and the always-safe device nodes
    ``/dev/zero`` / ``/dev/urandom`` (read) are granted. The executor adds
    the policy read paths and per-run grants on top of this.
    """
    return [
        (run_dir, fs_all_rights()),
        ("/dev/null", FS_READ_FILE | FS_WRITE_FILE),
        ("/dev/zero", FS_READ_FILE),
        ("/dev/urandom", FS_READ_FILE),
    ]


def apply_landlock_fd_rules(fd_rules: list[tuple[int, int]]) -> None:
    """Restrict the *current* process using pre-opened rule fds.

    This is the launcher path: fds are opened while the process is still
    privileged, so the rules can be installed after the identity drop.
    """
    if landlock_abi_version() is None:
        raise LandlockUnavailable("kernel does not support Landlock")
    libc = _libc()
    attr = struct.pack("Q", handled_fs_mask())
    ruleset_fd = libc.syscall(_SYS_CREATE_RULESET, attr, 8, 0)
    if ruleset_fd < 0:
        raise OSError(f"landlock_create_ruleset failed: {os.strerror(ctypes.get_errno())}")
    try:
        for parent_fd, rights in fd_rules:
            _add_fd_rule(libc, ruleset_fd, parent_fd, rights)
        _restrict_self(libc, ruleset_fd)
    finally:
        os.close(ruleset_fd)
    # Success: this process (and its future children) is now confined.


def _restrict_self(libc, ruleset_fd: int) -> None:
    # Restriction requires no_new_privs.
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(f"prctl(NO_NEW_PRIVS) failed: {os.strerror(ctypes.get_errno())}")
    rc = libc.syscall(_SYS_RESTRICT_SELF, ruleset_fd, 0)
    if rc != 0:
        raise OSError(f"landlock_restrict_self failed: {os.strerror(ctypes.get_errno())}")


def apply_landlock_rules(rules: list[tuple[str, int]]) -> None:
    """Restrict the *current* process with the given path rules.

    Opens each rule path itself — only safe while the process still has
    filesystem access to every target (e.g. before a privilege drop).
    Raises :class:`LandlockUnavailable` or :exc:`OSError`; on any failure
    the caller must NOT exec untrusted code.
    """
    if landlock_abi_version() is None:
        raise LandlockUnavailable("kernel does not support Landlock")
    libc = _libc()
    attr = struct.pack("Q", handled_fs_mask())
    ruleset_fd = libc.syscall(_SYS_CREATE_RULESET, attr, 8, 0)
    if ruleset_fd < 0:
        raise OSError(f"landlock_create_ruleset failed: {os.strerror(ctypes.get_errno())}")
    try:
        for path, rights in rules:
            _add_path_rule(libc, ruleset_fd, path, rights)
        _restrict_self(libc, ruleset_fd)
    finally:
        os.close(ruleset_fd)
    # Success: this process (and its future children) is now confined.


def apply_landlock(run_dir: str, rules: list[tuple[str, int]] | None = None) -> None:
    """Back-compatible wrapper: restrict the current process for ``run_dir``."""
    apply_landlock_rules(rules if rules is not None else default_rules(run_dir))
