"""Shared fixtures for the adversarial sandbox tests.

These tests compile and run REAL hostile programs. Nothing is mocked: the
assertions only pass if the sandbox actually contains the behavior.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from autograder.app.core.schemas import CompileConfig
from autograder.app.sandbox import SandboxExecutor, compile_c
from autograder.app.sandbox.executor import ExecLimits
from autograder.app.sandbox.landlock import landlock_abi_version


@pytest.fixture(scope="session")
def executor(tmp_path_factory):
    ex = SandboxExecutor(base_dir=tmp_path_factory.mktemp("sandbox"))
    ex.ensure_available()  # fail fast: this host cannot sandbox at all
    return ex


@pytest.fixture(scope="session")
def requires_landlock():
    if landlock_abi_version() != 1:
        pytest.skip("Landlock ABI v1 unavailable: filesystem-containment tests need it")


@pytest.fixture()
def c_binary(executor, tmp_path):
    """Compile a C source string; return the binary path. Fails loudly."""

    def _compile(name: str, source: str) -> str:
        src = tmp_path / f"{name}.c"
        src.write_text(source)
        outdir = tmp_path / f"{name}_out"
        res = compile_c(src, outdir, CompileConfig(), executor, allowed_dir=tmp_path)
        assert res.ok, f"fixture compile of {name} failed: {res.raw_stderr[:800]}"
        assert res.binary_path and Path(res.binary_path).is_file()
        return res.binary_path

    return _compile


@pytest.fixture()
def limits():
    return ExecLimits(wall_time_s=5.0, cpu_time_s=10.0, memory_mb=256, nproc=32)


def proc_cmdline_contains(marker: str) -> bool:
    """True if any live (non-zombie) process has ``marker`` in its cmdline."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode(errors="replace")
            if marker in cmdline:
                with open(f"/proc/{pid}/status") as fh:
                    state = [l for l in fh if l.startswith("State:")]
                if state and "Z (" not in state[0]:
                    return True
        except (OSError, FileNotFoundError):
            continue
    return False


def wait_until_gone(marker: str, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not proc_cmdline_contains(marker):
            return True
        time.sleep(0.1)
    return not proc_cmdline_contains(marker)
