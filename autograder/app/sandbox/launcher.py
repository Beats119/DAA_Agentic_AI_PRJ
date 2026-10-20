"""Sandbox launcher: applies the mandatory isolation policy, then execs.

Run by the executor as a FRESH, single-threaded process::

    [sys.executable, "-m", "autograder.app.sandbox.launcher", spec_path]

This replaces ``preexec_fn`` entirely. ``preexec_fn`` runs inside the
parent's ``fork()`` — in a multithreaded application (the coordinator and
the API both use thread pools) that is documented by CPython as unsafe:
only async-signal-safe operations may run between fork and exec, yet the
old sandbox applied rlimits, unshared namespaces, and loaded Landlock
rules there. A dedicated launcher process does the same work in a normal,
single-threaded interpreter where every operation is safe and every
failure can be reported precisely.

Protocol: the parent writes a JSON spec (argv, cwd, env, limits, policy,
landlock rules, status fd) to a file and passes the status pipe's write
end via ``pass_fds``. The launcher applies, IN ORDER:

a. Namespace + identity setup, by strategy:

   - ``root-setuid`` (our euid is 0): ``unshare(CLONE_NEWNET)`` directly
     — root may create a network namespace outright. The identity drop
     itself happens at step (c).
   - ``rootless-userns`` (any other euid — the normal deployment, e.g.
     the instructor's own account on WSL): a bare ``CLONE_NEWNET``
     unshare is EPERM for unprivileged users, so instead
     ``unshare(CLONE_NEWUSER | CLONE_NEWNET)`` creates both namespaces
     in one call — the netns is owned by the new user namespace, which
     is exactly what makes it creatable unprivileged. Then
     ``/proc/self/setgroups`` = "deny" (ignored only if the file does
     not exist), ``/proc/self/uid_map`` = ``0 <euid> 1`` and
     ``/proc/self/gid_map`` = ``0 <egid> 1``: inside the namespace the
     process is uid 0, on the host it remains the unprivileged app
     user. Any failure here is ``failed_step="user_namespace"`` and
     exit 125 — fail closed, exactly like every other mandatory step.
     Some hosts forbid even this (e.g. Ubuntu's AppArmor
     ``kernel.apparmor_restrict_unprivileged_userns=1``); then no
     evaluation runs at all — the sandbox never falls back to host
     networking.
b. rlimits (CPU / AS / FSIZE / NPROC / STACK, core dumps off).
c. Identity drop — root strategy only, and only when the policy says
   so: ``setgroups([])``, ``setgid``, ``setuid`` to the sandbox
   identity, then verify the process is no longer root. The rootless
   strategy never setuids (only the one mapped uid exists inside its
   namespace); its status reports ``uid`` as the OUTER (host) uid,
   plus ``uid_inner``/``uid_outer`` and ``strategy``.
d. ``prctl(PR_SET_NO_NEW_PRIVS, 1)``.
e. Landlock with the spec's allowlist rules (+ a read rule for the
   process's own ``/proc/self``, which sanitizers legitimately read;
   Landlock rules bind to the inode, and ``/proc/self`` is this very
   process before and after exec).
f. The seccomp profile from the policy (default action: EPERM).

It then writes ONE JSON status line to the status pipe (fd 3) —
``{"ok": true, "netns": ..., "landlock": ..., "seccomp": ...,
"resource_limits": ..., "no_new_privs": ..., "uid": ...,
"uid_dropped": ..., "strategy": ...}`` — and ``execve``s the target.
Any mandatory step that fails produces
``{"ok": false, "failed_step": ..., "error": ...}`` and exit code 125
BEFORE the target is exec'd: the target binary never runs uncontained.
"""

from __future__ import annotations

import ctypes
import json
import os
import resource
import sys
import typing

# All imports happen NOW, at interpreter startup, while the process is
# still fully privileged: after the identity drop (step c) the sandbox uid
# may be unable to read the application package, so nothing may be imported
# lazily later in main().
from .landlock import (
    FS_READ_DIR,
    FS_READ_FILE,
    apply_landlock_fd_rules,
    open_rule_fd,
)
from .seccomp import install_filter

_EXIT_SETUP_FAILED = 125
_PR_SET_NO_NEW_PRIVS = 38


def _apply_rlimits(limits: dict) -> None:
    mem = int(limits["memory_mb"]) * 1024 * 1024
    cpu = int(limits["cpu_time_s"])
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    if limits.get("unlimited_address_space"):
        # Sanitizer-instrumented binaries only: ASan reserves ~15 TB of
        # *virtual* space for shadow memory. Physical memory is still
        # capped by ASan's own hard_rss_limit_mb (set via ASAN_OPTIONS by
        # the caller) plus wall/CPU limits and the process-group kill.
        resource.setrlimit(resource.RLIMIT_AS, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    else:
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    fsize = int(limits["fsize_mb"]) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    resource.setrlimit(resource.RLIMIT_NPROC, (int(limits["nproc"]), int(limits["nproc"])))
    stack = int(limits["stack_mb"]) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_STACK, (stack, stack))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: launcher <spec.json>", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)

    # Normalise the status pipe to fd 3 (the documented protocol fd). The
    # parent passes the real fd number in the spec; dup2 before any
    # restriction is applied, while the process is fully capable.
    status_fd = int(spec["status_fd"])
    if status_fd != 3:
        os.dup2(status_fd, 3)
        status_fd = 3

    policy = spec["policy"]
    mandatory = set(policy.get("mandatory", []))

    def fail(step: str, error: object) -> "typing.NoReturn":
        try:
            payload = {"ok": False, "failed_step": step, "error": str(error)}
            os.write(status_fd, (json.dumps(payload) + "\n").encode())
        except OSError:
            pass
        print(f"sandbox launcher: mandatory step {step!r} failed: {error}", file=sys.stderr, flush=True)
        os._exit(_EXIT_SETUP_FAILED)

    # Pre-open every Landlock rule fd NOW, while fully privileged. The
    # rules themselves are installed at step (e) — after the identity
    # drop — using these fds: a path opened after setuid could fail on
    # any ancestor directory the sandbox uid cannot traverse, but an
    # already-open O_PATH fd is immune to later DAC changes.
    rule_fds: list[tuple[int, int]] = []
    if spec.get("want_landlock", True):
        try:
            for path, rights in spec["landlock_rules"]:
                rule_fds.append((open_rule_fd(str(path)), int(rights)))
            proc_self = os.path.realpath("/proc/self")
            if os.path.isdir(proc_self):
                rule_fds.append(
                    (open_rule_fd(proc_self), FS_READ_FILE | FS_READ_DIR)
                )
        except OSError as exc:
            if "landlock_fs" in mandatory:
                fail("landlock_fs", exc)
            rule_fds = []

    # Our identity BEFORE any namespace games: after
    # unshare(CLONE_NEWUSER), getuid() reports the overflow uid (65534)
    # until a uid_map is written, so the outer ids must be captured now.
    euid, egid = os.geteuid(), os.getegid()
    strategy = "root-setuid" if euid == 0 else "rootless-userns"
    uid_inner: int | None = None
    uid_outer: int | None = None

    # a. Namespace setup — the strategy depends on our privileges.
    #    Root: a bare CLONE_NEWNET unshare succeeds outright.
    #    Non-root: CLONE_NEWNET alone is EPERM (creating a netns needs
    #    CAP_SYS_ADMIN in the CURRENT user namespace). Creating a user
    #    namespace in the SAME unshare call makes the netns owned by
    #    that new namespace, where we DO hold the capability — the
    #    standard rootless-container pattern.
    netns = False
    if spec.get("want_netns", True):
        if euid == 0:
            try:
                os.unshare(os.CLONE_NEWNET)
                netns = True
            except OSError as exc:
                if "network_namespace" in mandatory:
                    fail("network_namespace", exc)
        else:
            try:
                os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)
                try:
                    with open("/proc/self/setgroups", "w") as fh:
                        fh.write("deny")
                except FileNotFoundError:
                    pass  # kernels without the setgroups file
                with open("/proc/self/uid_map", "w") as fh:
                    fh.write(f"0 {euid} 1")
                with open("/proc/self/gid_map", "w") as fh:
                    fh.write(f"0 {egid} 1")
            except OSError as exc:
                # Mandatory in effect: without the namespace there is
                # no network isolation for an unprivileged launcher.
                # Fail closed (exit 125) rather than run on the host net.
                fail("user_namespace", exc)
            netns = True
            uid_inner, uid_outer = 0, euid
    elif "network_namespace" in mandatory:
        fail("network_namespace", "executor configured without network namespace")

    # b. Resource limits.
    rlimits_ok = False
    try:
        _apply_rlimits(spec["limits"])
        rlimits_ok = True
    except (OSError, ValueError) as exc:
        if "resource_limits" in mandatory:
            fail("resource_limits", exc)

    # c. Dedicated unprivileged identity (root strategy only). Dropping
    #    from root to the sandbox uid also makes RLIMIT_NPROC meaningful
    #    (it is not enforced for uid 0) and removes every capability the
    #    app user holds. The rootless strategy never setuids: inside its
    #    user namespace only the single mapped uid exists, and the
    #    process already holds no privilege on the host.
    uid_dropped = False
    uid_drop_reason: str | None = None
    if euid == 0 and policy.get("drop_privileges_when_root", True):
        try:
            os.setgroups([])
            os.setgid(int(policy["sandbox_gid"]))
            os.setuid(int(policy["sandbox_uid"]))
        except OSError as exc:
            fail("setuid", exc)
        if os.getuid() == 0 or os.geteuid() == 0:
            fail("setuid", "process is still root after the identity drop")
        uid_dropped = True
    elif euid != 0:
        uid_drop_reason = (
            "rootless user namespace: the child is uid 0 inside its own "
            "user+network namespaces, mapped to the unprivileged app uid "
            "on the host; no setuid is possible or needed"
        )
    else:
        uid_drop_reason = "policy disabled the identity drop"

    # d. no_new_privs — also a precondition for Landlock and seccomp.
    nnp = False
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(os.strerror(ctypes.get_errno()))
        nnp = True
    except OSError as exc:
        if "no_new_privs" in mandatory:
            fail("no_new_privs", exc)

    # e. Landlock filesystem allowlist (rules computed by the executor from
    #    the policy; fds pre-opened above, before the identity drop).
    landlock_ok = False
    if spec.get("want_landlock", True):
        try:
            apply_landlock_fd_rules(rule_fds)
            landlock_ok = True
        except Exception as exc:
            if "landlock_fs" in mandatory:
                fail("landlock_fs", exc)
    elif "landlock_fs" in mandatory:
        fail("landlock_fs", "executor configured without Landlock")

    # f. Seccomp syscall allowlist.
    seccomp_ok = False
    try:
        install_filter(str(policy["seccomp_profile"]))
        seccomp_ok = True
    except Exception as exc:
        if "seccomp_filter" in mandatory:
            fail("seccomp_filter", exc)

    # g. Report ACTUAL enforcement, then become the target program.
    #    ``uid`` is the HOST-meaningful identity: for the rootless
    #    strategy getuid() here would report the namespace-inner 0,
    #    which would falsely read as "ran as host root" — report the
    #    outer uid (with uid_inner/uid_outer alongside for full detail).
    status = {
        "ok": True,
        "netns": netns,
        "landlock": landlock_ok,
        "seccomp": seccomp_ok,
        "resource_limits": rlimits_ok,
        "no_new_privs": nnp,
        "uid": uid_outer if uid_outer is not None else os.getuid(),
        "uid_dropped": uid_dropped,
        "uid_drop_reason": uid_drop_reason,
        "strategy": strategy,
    }
    if uid_inner is not None:
        status["uid_inner"] = uid_inner
        status["uid_outer"] = uid_outer
    os.write(status_fd, (json.dumps(status) + "\n").encode())
    try:
        os.close(status_fd)
    except OSError:
        pass
    target_argv = [str(a) for a in spec["argv"]]
    try:
        os.execve(target_argv[0], target_argv, dict(spec["env"]))
    except OSError as exc:
        # The parent already has an "ok" status; exit with the setup-failure
        # code so the run is visibly a sandbox/setup failure, never a
        # silently successful execution.
        print(f"sandbox launcher: execve({target_argv[0]!r}) failed: {exc}", file=sys.stderr, flush=True)
        os._exit(_EXIT_SETUP_FAILED)
    return _EXIT_SETUP_FAILED  # unreachable


if __name__ == "__main__":
    sys.exit(main(sys.argv))
