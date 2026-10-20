"""Sandbox security policy — what MUST hold before untrusted code runs.

The policy is the contract between configuration and the executor:

- ``mandatory`` names the mechanisms that are *conditions of execution*.
  If any mandatory mechanism cannot be established for a run, the run is
  REFUSED (``SandboxUnavailable`` / ``SandboxError``) — the sandbox never
  silently downgrades to weaker isolation while implying it is secure.
- ``fs_read_paths`` is the *complete* filesystem read allowlist for the
  runtime profile (plus the per-run directory and ``/dev/null``, which the
  executor always adds). It deliberately contains ONLY the runtime
  libraries and the dynamic loader's directories — never ``/``, never
  ``/etc``, never a home directory.
- Children execute as a dedicated unprivileged identity
  (``sandbox_uid``/``sandbox_gid``, default ``nobody``) whenever the
  application itself runs as root.

Policy loading: :func:`load_policy` reads JSON from the path in the
``AUTOGRADER_SANDBOX_POLICY`` environment variable when set (this wires the
previously-dead ``SANDBOX_POLICY`` concept from ``.env.example``). An
unreadable or invalid policy file raises — it never silently falls back to
defaults, because a silently-ignored strict policy is worse than none.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import Field

from ..core.schemas import StrictModel

POLICY_ENV_VAR = "AUTOGRADER_SANDBOX_POLICY"

#: Capability names the executor knows how to enforce/report. A policy that
#: mandates anything outside this set is refused as unintelligible rather
#: than partially honoured.
KNOWN_CAPABILITIES = frozenset(
    {
        "resource_limits",
        "landlock_fs",
        "network_namespace",
        "seccomp_filter",
        "no_new_privs",
        "process_group_kill",
    }
)

DEFAULT_MANDATORY = [
    "resource_limits",
    "landlock_fs",
    "network_namespace",
    "seccomp_filter",
    "no_new_privs",
]

#: Runtime libraries + dynamic loader ONLY. A dynamically linked C program
#: needs the loader (``/lib64``), libc and friends (``/lib``, ``/usr/lib``).
#: Deliberately absent: ``/etc`` (ld.so falls back to scanning the trusted
#: directories when ``/etc/ld.so.cache`` is unreadable — slower, still
#: correct), ``/usr/bin`` (only the invoked binary is granted, per-file),
#: and every home/workspace path.
DEFAULT_FS_READ_PATHS = ["/lib", "/lib64", "/usr/lib", "/usr/lib64"]


class SandboxPolicyError(ValueError):
    """The sandbox policy file is unreadable or invalid. Fatal, by design."""


class SandboxPolicy(StrictModel):
    """Enforced sandbox policy. See module docstring."""

    mandatory: list[str] = Field(default_factory=lambda: list(DEFAULT_MANDATORY))
    sandbox_uid: int = 65534  # nobody
    sandbox_gid: int = 65534  # nogroup
    drop_privileges_when_root: bool = True
    fs_read_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_FS_READ_PATHS))
    seccomp_profile: str = "runtime"  # default profile; callers may override per run


def load_policy() -> SandboxPolicy:
    """Load the effective policy.

    Reads ``$AUTOGRADER_SANDBOX_POLICY`` (a JSON file) when set; otherwise
    returns the built-in defaults. Raises :class:`SandboxPolicyError` when
    the variable is set but the file cannot be read or validated.
    """
    raw = os.environ.get(POLICY_ENV_VAR)
    if not raw:
        return SandboxPolicy()
    path = Path(raw)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SandboxPolicyError(
            f"{POLICY_ENV_VAR}={raw!r}: cannot read sandbox policy file: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SandboxPolicyError(
            f"{POLICY_ENV_VAR}={raw!r}: invalid JSON in sandbox policy file: {exc}"
        ) from exc
    try:
        return SandboxPolicy.model_validate(data)
    except Exception as exc:  # pydantic ValidationError — surface, never default
        raise SandboxPolicyError(
            f"{POLICY_ENV_VAR}={raw!r}: sandbox policy failed validation: {exc}"
        ) from exc
