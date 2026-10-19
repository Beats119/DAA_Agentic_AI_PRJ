"""Untrusted C compilation inside the sandbox.

Compiling student code is itself an untrusted operation (a malicious source
can exploit compiler bugs or simply hang the compiler), so the compiler runs
under the same sandbox policy with generous limits. ``TMPDIR`` is pointed at
the run directory because the Landlock policy denies writes to ``/tmp``.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from ..core.schemas import CompileConfig, StrictModel
from .executor import CompilerLimits, ExecLimits, SandboxError, SandboxExecutor

_DIAG_RE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<col>\d+)?:?\s*"
    r"(?P<kind>error|warning|note):\s*(?P<msg>.*)$"
)
_BARE_RE = re.compile(r"^(?P<kind>error|warning):\s*(?P<msg>.*)$")


class Diagnostic(StrictModel):
    kind: Literal["error", "warning", "note", "other"]
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None


class CompileResult(StrictModel):
    ok: bool
    returncode: int | None
    diagnostics: list[Diagnostic]
    warnings: list[Diagnostic] = []
    raw_stderr: str
    raw_stdout: str
    binary_path: str | None = None
    binary_sha256: str | None = None
    wall_s: float = 0.0
    timed_out: bool = False
    compiler: str = ""

    @model_validator(mode="before")
    @classmethod
    def _split_warnings(cls, data):
        # Populate the warnings convenience field before validation, so the
        # StrictModel's validate_assignment never re-triggers this validator
        # (which caused infinite recursion with mode="after").
        if isinstance(data, dict) and "warnings" not in data:
            diags = data.get("diagnostics") or []
            def _kind(d):
                return d.get("kind") if isinstance(d, dict) else d.kind
            data["warnings"] = [d for d in diags if _kind(d) == "warning"]
        return data


def parse_diagnostics(text: str) -> list[Diagnostic]:
    """Best-effort parse of gcc/clang diagnostics. Raw text is always kept."""
    out: list[Diagnostic] = []
    for line in text.splitlines():
        m = _DIAG_RE.match(line.strip())
        if m:
            out.append(
                Diagnostic(
                    kind=m.group("kind"),
                    message=m.group("msg").strip(),
                    file=m.group("file"),
                    line=int(m.group("line")),
                    column=int(m.group("col")) if m.group("col") else None,
                )
            )
            continue
        m = _BARE_RE.match(line.strip())
        if m:
            out.append(Diagnostic(kind=m.group("kind"), message=m.group("msg").strip()))
    return out


def compile_c(
    source_path: str | Path,
    out_dir: str | Path,
    config: CompileConfig,
    executor: SandboxExecutor,
    *,
    allowed_dir: str | Path | None = None,
    limits: ExecLimits | None = None,
) -> CompileResult:
    """Compile one C file in the sandbox.

    ``source_path`` must live under ``allowed_dir`` when given (path-traversal
    guard). The binary is written to ``out_dir`` with a unique name.
    """
    source = Path(source_path).resolve()
    if allowed_dir is not None:
        allowed = Path(allowed_dir).resolve()
        if not source.is_relative_to(allowed):
            raise SandboxError(
                f"source {source} is outside the allowed directory {allowed}"
            )
    if not source.is_file():
        raise SandboxError(f"source file not found: {source}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmpdir = out / "tmp"
    tmpdir.mkdir(exist_ok=True)

    binary = out / f"{source.stem}_{uuid.uuid4().hex[:8]}"
    argv = [config.compiler, f"-std={config.std}", *config.flags, str(source), "-o", str(binary)]
    start = time.monotonic()
    result = executor.run(
        argv,
        "",
        limits or CompilerLimits(),
        extra_env={"TMPDIR": str(tmpdir)},
        writable_paths=[str(out)],
    )
    wall_s = time.monotonic() - start

    diagnostics = parse_diagnostics(result.stderr)
    ok = (
        not result.timed_out
        and result.exit_code == 0
        and binary.is_file()
        and not any(d.kind == "error" for d in diagnostics)
    )
    sha = None
    if ok:
        sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    return CompileResult(
        ok=ok,
        returncode=result.exit_code,
        diagnostics=diagnostics,
        raw_stderr=result.stderr,
        raw_stdout=result.stdout,
        binary_path=str(binary) if ok else None,
        binary_sha256=sha,
        wall_s=wall_s,
        timed_out=result.timed_out,
        compiler=config.compiler,
    )
