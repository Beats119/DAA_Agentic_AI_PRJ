"""Landlock filesystem containment for unprivileged processes.

Implemented with ctypes against the raw syscalls (no libseccomp / no
privilege needed). Applied in the sandboxed child's ``preexec`` step,
immediately before ``exec`` — once restricted, the process cannot lift the
restriction, and neither can its children.

Policy (ABI v1):
- ``/``                       : read + list + execute (libs, loader, binaries)
- ``run_dir`` (per-run temp)  : full access (the only writable area)
- ``/dev/null``               : read + write (harmless, commonly used)
- ``/dev/zero``, ``/dev/urandom`` : read

Everything else not covered by a rule is denied (Landlock is deny-by-default
for handled access types). In particular a sandboxed program CANNOT create or
write files anywhere outside its run directory — ``/tmp``, the user's home,
or ``../../escape``-style relative paths all fail with EACCES.

Raises :class:`LandlockUnavailable` if the kernel does not support it, and
:exc:`OSError` if any step of applying the policy fails (fail closed: the
caller must not exec the untrusted binary in that case).
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

_HANDLED_V1 = 0x3FFF  # all ABI v1 fs rights, incl. FS_REFER (kernel accepts this)


class LandlockUnavailable(RuntimeError):
    """The running kernel does not support Landlock."""


def _libc():
    return ctypes.CDLL("libc.so.6", use_errno=True)


@functools.lru_cache(maxsize=1)
def landlock_abi_version() -> int | None:
    """Probe the supported Landlock ABI version (cached). Only v1 is driven."""
    libc = _libc()
    pid = os.fork()
    if pid == 0:  # child
        try:
            attr = struct.pack("Q", _HANDLED_V1)
            fd = libc.syscall(_SYS_CREATE_RULESET, attr, 8, 0)
            if fd < 0:
                os._exit(1)
            os.close(fd)
            os._exit(0)
        except Exception:
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    if os.waitstatus_to_exitcode(status) == 0:
        return 1
    return None


def _add_path_rule(libc, ruleset_fd: int, path: str, rights: int) -> None:
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        # struct landlock_path_beneath_attr (packed): u64 allowed_access, s32 parent_fd
        attr = struct.pack("Qi", rights, fd)
        rc = libc.syscall(_SYS_ADD_RULE, ruleset_fd, _RULE_PATH_BENEATH, attr, 0)
        if rc != 0:
            raise OSError(f"landlock_add_rule({path!r}) failed: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(fd)


def default_rules(run_dir: str) -> list[tuple[str, int]]:
    """The standard sandbox filesystem policy for one run directory."""
    return [
        ("/", FS_READ_EXEC),
        (run_dir, FS_RW_DIR),
        ("/dev/null", FS_READ_FILE | FS_WRITE_FILE),
        ("/dev/zero", FS_READ_FILE),
        ("/dev/urandom", FS_READ_FILE),
    ]


def apply_landlock(run_dir: str, rules: list[tuple[str, int]] | None = None) -> None:
    """Restrict the *current* process with Landlock. No return on success.

    Must be called after all setup that needs filesystem access (it is the
    last step before exec). Raises :class:`LandlockUnavailable` or
    :exc:`OSError`; on any failure the caller must NOT exec untrusted code.
    """
    if landlock_abi_version() != 1:
        raise LandlockUnavailable("kernel does not support Landlock ABI v1")
    libc = _libc()
    rules = rules if rules is not None else default_rules(run_dir)
    attr = struct.pack("Q", _HANDLED_V1)
    ruleset_fd = libc.syscall(_SYS_CREATE_RULESET, attr, 8, 0)
    if ruleset_fd < 0:
        raise OSError(f"landlock_create_ruleset failed: {os.strerror(ctypes.get_errno())}")
    try:
        for path, rights in rules:
            _add_path_rule(libc, ruleset_fd, path, rights)
        # Restriction requires no_new_privs.
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(f"prctl(NO_NEW_PRIVS) failed: {os.strerror(ctypes.get_errno())}")
        rc = libc.syscall(_SYS_RESTRICT_SELF, ruleset_fd, 0)
        if rc != 0:
            raise OSError(f"landlock_restrict_self failed: {os.strerror(ctypes.get_errno())}")
    finally:
        os.close(ruleset_fd)
    # Success: this process (and its future children) is now confined.
