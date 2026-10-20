"""Memory-safety analyzer: AddressSanitizer + UBSan runs.

Builds the student source with ``-fsanitize=address,undefined`` in the
sandbox, runs the trusted test inputs against the instrumented binary, and
parses sanitizer diagnostics into findings. A clean run scores 1.0; each
finding degrades per severity.

If the sanitizer build cannot run on the host (toolchain lacks the
runtime), the result is ``unsupported`` with an explicit reason — never a
silent pass or fail. A build failure caused by the *submission* (it does
not compile under the sanitizer flags) is ``failed`` instead: that is the
student's outcome, not a host limitation (D12).

Sandbox interaction note: ASan reserves ~15 TB of *virtual* address space
for its shadow memory and cannot start under a normal RLIMIT_AS cap, so
instrumented runs use ``unlimited_address_space=True``. Physical memory is
still bounded by ASan's own ``hard_rss_limit_mb`` (default 512 MB, via
ASAN_OPTIONS), plus the usual wall-time timeout and process-group kill.
This trade-off is explicit and documented — never enable unlimited address
space for non-instrumented untrusted binaries.

Leak checking note: ASAN_OPTIONS sets ``detect_leaks=0``, so memory leaks
are NOT detected by this analyzer — only memory errors (ASan) and
undefined behavior (UBSan) are. Both this and the unlimited address space
are disclosed in every completed result's ``limitations`` (D12).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ...core.schemas import CompileConfig
from ...sandbox.executor import ExecLimits
from ..base import AnalyzerContext, build_result

ANALYZER_ID = "memory_safety"

DEFAULTS = {
    "finding_penalty": 0.25,
    "score_floor": 0.0,
    "max_findings_recorded": 10,
    "asan_rss_limit_mb": 512,
    # ASan reserves ~15 TB of *virtual* address space for shadow memory, so
    # RLIMIT_AS must be unlimited for instrumented runs. Physical memory is
    # still capped by ASan itself (hard_rss_limit_mb) plus wall-time/killpg.
}

# Matches lines like:
#   ==123==ERROR: AddressSanitizer: heap-buffer-overflow on address ...
#   runtime error: index 5 out of bounds for type 'int [5]'
_ASAN_RE = re.compile(r"ERROR:\s*(\w+):\s*([^\n]+)")
_UBSAN_RE = re.compile(r"runtime error:\s*([^\n]+)")
_STACK_RE = re.compile(r"#\d+\s+0x[0-9a-f]+\s+in\s+(\S+)")


def _parse_sanitizer_output(stderr: str) -> list[dict[str, Any]]:
    """Extract structured findings from ASan/UBSan stderr. Pure function."""
    findings: list[dict[str, Any]] = []
    for m in _ASAN_RE.finditer(stderr):
        sanitizer, check = m.group(1), m.group(2).strip()
        # Grab the first stack frame naming a function, if present.
        tail = stderr[m.end() : m.end() + 2000]
        frame = _STACK_RE.search(tail)
        findings.append(
            {
                "sanitizer": sanitizer,
                "check": check[:200],
                "stack_hint": frame.group(1)[:120] if frame else None,
            }
        )
    for m in _UBSAN_RE.finditer(stderr):
        findings.append(
            {
                "sanitizer": "UBSan",
                "check": m.group(1).strip()[:200],
                "stack_hint": None,
            }
        )
    return findings


def _build_sanitizer_binary(ctx: AnalyzerContext, out_dir: Path):
    """Compile with sanitizers inside the sandbox. Returns CompileResult."""
    base = ctx.rules.compile
    san_config = CompileConfig(
        compiler=base.compiler,
        # Keep warnings on; drop -O2 in favor of -O1 -g for usable traces.
        flags=["-O1", "-g", "-Wall", "-fsanitize=address,undefined",
               "-fno-omit-frame-pointer"],
        std=base.std,
    )
    return ctx.compiler(
        ctx.source_path,
        out_dir,
        san_config,
        ctx.executor,
        allowed_dir=str(Path(ctx.source_path).resolve().parent),
    )


def analyze(ctx: AnalyzerContext) -> Any:
    params = {**DEFAULTS, **ctx.metric.params}
    run_dir = Path(ctx.run_dir)
    san_dir = run_dir / "sanitizer_build"

    try:
        san_result = _build_sanitizer_binary(ctx, san_dir)
    except Exception as exc:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="unsupported",
            diagnostics={"reason": f"sanitizer build could not run: {exc}"},
            limitations=[
                "AddressSanitizer/UBSan instrumentation is unavailable on this "
                "host — memory safety cannot be assessed"
            ],
        )

    if not san_result.ok or not san_result.binary_path:
        raw = (san_result.raw_stderr or "") + (san_result.raw_stdout or "")
        missing_runtime = any(
            tok in raw
            for tok in ("libasan", "libubsan", "unrecognized", "not found", "cannot find")
        )
        if missing_runtime:
            # Sanitizer runtime missing from the toolchain -> unsupported, honest.
            return build_result(
                metric=ctx.metric,
                analyzer_id=ANALYZER_ID,
                status="unsupported",
                diagnostics={
                    "reason": "sanitizer runtime unavailable in this toolchain",
                    "raw_tail": raw[-1500:],
                },
                limitations=[
                    "memory-safety analysis requires a working "
                    "-fsanitize=address,undefined toolchain"
                ],
            )
        # D12: the toolchain works but the SUBMISSION does not compile
        # under the sanitizer build — a student failure, not a host
        # limitation. Never mislabel it `unsupported`.
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="failed",
            diagnostics={
                "reason": "sanitizer-instrumented build failed: the "
                          "submission does not compile under "
                          "-fsanitize=address,undefined",
                "raw_tail": raw[-1500:],
            },
            limitations=[
                "memory safety could not be assessed because the "
                "sanitizer-instrumented build of the submission failed"
            ],
        )

    binary = san_result.binary_path
    trusted = [tc for tc in ctx.test_cases if tc.status == "trusted"]
    if not trusted:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            diagnostics={"reason": "no trusted test inputs to drive the sanitizer runs"},
            warnings=["no trusted tests — sanitizer runs need inputs to execute"],
        )

    limits = ExecLimits(
        wall_time_s=float(ctx.rules.limits.time_limit_s),
        cpu_time_s=float(ctx.rules.limits.time_limit_s) * 4,
        memory_mb=int(ctx.rules.limits.memory_limit_mb),
        # ASan needs terabytes of *virtual* address space; the physical cap
        # comes from ASan's own hard_rss_limit_mb (see asan_env below).
        unlimited_address_space=True,
        nproc=ctx.rules.limits.process_limit,
        output_bytes=ctx.rules.limits.output_limit_bytes,
    )
    asan_env = {
        "ASAN_OPTIONS": (
            f"hard_rss_limit_mb={int(params['asan_rss_limit_mb'])}:"
            "abort_on_error=1:detect_leaks=0"
        ),
        # UBSan prints by default; keep halt_on_error off so one bad test
        # doesn't hide the rest.
        "UBSAN_OPTIONS": "halt_on_error=0:print_stacktrace=1",
    }
    all_findings: list[dict[str, Any]] = []
    per_test: list[dict[str, Any]] = []
    for tc in trusted:
        try:
            res = ctx.executor.run([binary], tc.input, limits, extra_env=asan_env)
        except Exception as exc:
            per_test.append({"test_id": tc.id, "error": str(exc)[:120]})
            continue
        parsed = _parse_sanitizer_output(res.stderr)
        for f in parsed:
            f["test_id"] = tc.id
        all_findings.extend(parsed)
        per_test.append(
            {"test_id": tc.id, "findings": len(parsed), "exit_code": res.exit_code,
             "signal": res.signal}
        )

    recorded = all_findings[: int(params["max_findings_recorded"])]
    penalty = float(params["finding_penalty"])
    score = max(float(params["score_floor"]), 1.0 - penalty * len(all_findings))

    evidence_refs = [
        ctx.add_evidence(
            ANALYZER_ID,
            "sanitizer_findings",
            {
                "finding_count": len(all_findings),
                "findings": recorded,
                "truncated": len(all_findings) > len(recorded),
                "per_test": per_test,
                "sanitizer_binary_sha256": san_result.binary_sha256,
            },
            severity="error" if all_findings else None,
            confidence=0.95,
        )
    ]
    findings_text = [
        f"[{f['sanitizer']}] {f['check']}"
        + (f" (in {f['stack_hint']}, test {f['test_id']})" if f.get("stack_hint") else f" (test {f['test_id']})")
        for f in recorded
    ]

    return build_result(
        metric=ctx.metric,
        analyzer_id=ANALYZER_ID,
        status="completed",
        score=score,
        measurements={
            "finding_count": len(all_findings),
            "tests_run": len(trusted),
        },
        evidence_refs=evidence_refs,
        findings=findings_text,
        limitations=[
            "sanitizers detect issues only on executed paths — clean runs do "
            "not prove the absence of memory bugs",
            # D12 disclosures — both facts change what this result means.
            "the address-space limit (RLIMIT_AS) is disabled for the "
            "sanitizer runs: ASan reserves ~15 TB of virtual address space "
            "for shadow memory and cannot start under a normal cap; "
            "physical memory remains bounded by ASan's "
            f"hard_rss_limit_mb={int(params['asan_rss_limit_mb'])} plus "
            "wall-time and process limits",
            "leak detection is disabled (ASAN_OPTIONS detect_leaks=0): "
            "memory leaks are NOT checked — only memory errors "
            "(AddressSanitizer) and undefined behavior (UBSan) are",
        ],
        execution_metadata={"sanitizer_binary_sha256": san_result.binary_sha256},
    )
