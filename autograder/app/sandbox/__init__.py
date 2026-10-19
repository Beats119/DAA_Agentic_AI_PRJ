"""Isolated native Linux compilation and execution (Phase 4)."""

from .capabilities import SandboxCapabilities, probe_capabilities
from .compiler import CompileResult, Diagnostic, compile_c, parse_diagnostics
from .executor import (
    CompilerLimits,
    EnforcementReport,
    ExecLimits,
    ExecutionResult,
    SandboxError,
    SandboxExecutor,
    SandboxUnavailable,
    SANDBOX_VERSION,
)
from .landlock import LandlockUnavailable, apply_landlock, landlock_abi_version

__all__ = [
    "SANDBOX_VERSION",
    "CompileResult",
    "CompilerLimits",
    "Diagnostic",
    "EnforcementReport",
    "ExecLimits",
    "ExecutionResult",
    "LandlockUnavailable",
    "SandboxCapabilities",
    "SandboxError",
    "SandboxExecutor",
    "SandboxUnavailable",
    "apply_landlock",
    "compile_c",
    "landlock_abi_version",
    "parse_diagnostics",
    "probe_capabilities",
]
