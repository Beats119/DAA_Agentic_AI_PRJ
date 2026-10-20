"""Static-analysis analyzer.

Primary: deterministic built-in checks (tool ``"builtin"``) — simple,
explainable, never fabricated. Supplementary: cppcheck when installed
(tool ``"cppcheck"``), merged and declared. cppcheck parses UNTRUSTED
source, so it runs inside the sandbox executor (compiler seccomp
profile, Landlock read access to the source directory only) — never as
a bare host subprocess. If neither applies, the result is
``unsupported`` — never a silent perfect score.

Score: 1.0 - sum(severity penalties), floored at ``score_floor``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ...sandbox.executor import ExecLimits
from ..base import AnalyzerContext, build_result
from .checks import (
    BUILTIN_CHECK_IDS,
    SEVERITY_PENALTY,
    StaticFinding,
    run_builtin_checks,
)

ANALYZER_ID = "static_analysis"

# Human-readable label for each cppcheck outcome, used in `checks_run`.
_CPPCHECK_STATE_LABEL = {
    "ran": "ran in sandbox",
    "not_installed": "not installed",
    "no_executor": "unavailable (no sandbox executor)",
    "timed_out": "timed out in sandbox",
    "failed": "failed in sandbox",
    "not_attempted": "not attempted (disabled)",
}

DEFAULTS = {
    "score_floor": 0.0,
    "severity_penalty": dict(SEVERITY_PENALTY),
    "use_cppcheck": True,
    "cppcheck_timeout_s": 30,
}


def _run_cppcheck(
    source_path: str, timeout_s: float, executor: Any
) -> tuple[list[StaticFinding] | None, str, str]:
    """Run cppcheck if installed, inside the sandbox.

    Returns ``(findings, status, detail)``. ``findings`` is None unless
    the run actually happened; ``status`` is one of ``ran``,
    ``not_installed``, ``no_executor``, ``timed_out``, ``failed`` and
    ``detail`` says exactly what happened, so the analyzer can report it
    instead of collapsing every failure into a vague "unavailable"
    (cppcheck is never run unsandboxed; a failed/timed-out run is not an
    analyzer error — the built-in checks still ran).
    """
    exe = shutil.which("cppcheck")
    if exe is None:
        return None, "not_installed", "cppcheck is not installed on this host"
    if executor is None:
        return (
            None,
            "no_executor",
            "no sandbox executor available — cppcheck is never run unsandboxed",
        )
    read_paths = [str(Path(source_path).parent)]
    # cppcheck loads its cfg files relative to the install prefix.
    for cfg_dir in (str(Path(exe).resolve().parent.parent / "share" / "cppcheck"), "/usr/share/cppcheck"):
        if Path(cfg_dir).is_dir():
            read_paths.append(cfg_dir)
    try:
        result = executor.run(
            [exe, "--enable=warning,performance", "--quiet", "--template={line}:{severity}:{id}:{message}", source_path],
            "",
            ExecLimits(wall_time_s=timeout_s, cpu_time_s=timeout_s + 30.0, output_bytes=2_000_000),
            read_paths=read_paths,
            seccomp_profile="compiler",
        )
    except Exception as exc:
        return (
            None,
            "failed",
            f"cppcheck run failed in the sandbox ({type(exc).__name__}: {exc})",
        )
    if result.timed_out:
        return None, "timed_out", f"cppcheck timed out after {timeout_s:g}s in the sandbox"
    if result.exit_code not in (0, None):
        return (
            None,
            "failed",
            f"cppcheck exited with code {result.exit_code} in the sandbox",
        )
    findings: list[StaticFinding] = []
    for line in (result.stderr or "").splitlines():
        parts = line.split(":", 3)
        if len(parts) != 4:
            continue
        lineno, severity, check_id, message = parts
        sev = {"error": "high", "warning": "medium"}.get(severity.strip(), "low")
        try:
            ln = int(lineno)
        except ValueError:
            ln = 0
        findings.append(
            StaticFinding(
                check_id=f"cppcheck.{check_id.strip()}",
                severity=sev,
                line=ln,
                message=message.strip(),
                tool="cppcheck",
            )
        )
    return findings, "ran", "cppcheck ran inside the sandbox (compiler profile)"


def analyze(ctx: AnalyzerContext) -> Any:
    params = {**DEFAULTS, **ctx.metric.params}
    try:
        source = Path(ctx.source_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="failed",
            diagnostics={"reason": f"could not read source: {exc}"},
        )

    builtin = run_builtin_checks(source)
    cppcheck_findings: list[StaticFinding] = []
    cppcheck_status = "not_attempted"  # coarse status (ran|unavailable|not_attempted)
    cppcheck_state = "not_attempted"  # exact outcome of the cppcheck step
    cppcheck_detail = "cppcheck disabled by metric params (use_cppcheck=false)"
    if params["use_cppcheck"]:
        found, cppcheck_state, cppcheck_detail = _run_cppcheck(
            ctx.source_path, float(params["cppcheck_timeout_s"]), ctx.executor
        )
        if found is None:
            cppcheck_status = "unavailable"
        else:
            cppcheck_status = "ran"
            cppcheck_findings = found

    # Exactly which checks were performed — a result must never imply
    # cppcheck ran when it did not.
    checks_run = list(BUILTIN_CHECK_IDS) + [
        "cppcheck: " + _CPPCHECK_STATE_LABEL[cppcheck_state]
    ]

    all_findings = builtin + cppcheck_findings
    limitations: list[str] = []
    if cppcheck_state == "not_installed":
        limitations.append(
            "cppcheck is not installed on this host — only built-in checks ran "
            "(install cppcheck for deeper analysis)"
        )
    elif cppcheck_state == "no_executor":
        limitations.append(
            "cppcheck was not run: no sandbox executor available and "
            "cppcheck is never run unsandboxed — only built-in checks ran"
        )
    elif cppcheck_state == "timed_out":
        limitations.append(
            "cppcheck timed out inside the sandbox — only built-in checks ran"
        )
    elif cppcheck_state == "failed":
        limitations.append(
            "cppcheck failed inside the sandbox — only built-in checks ran"
        )

    penalties = params["severity_penalty"]
    total_penalty = sum(float(penalties.get(f.severity, 0.05)) for f in all_findings)
    score = max(float(params["score_floor"]), 1.0 - total_penalty)

    evidence_refs = [
        ctx.add_evidence(
            ANALYZER_ID,
            "static_findings",
            {
                "tool_breakdown": {
                    "builtin": len(builtin),
                    "cppcheck": len(cppcheck_findings),
                    "cppcheck_status": cppcheck_status,
                    "cppcheck_state": cppcheck_state,
                    "cppcheck_detail": cppcheck_detail,
                },
                "checks_run": checks_run,
                "findings": [
                    {
                        "check_id": f.check_id,
                        "severity": f.severity,
                        "line": f.line,
                        "message": f.message,
                        "tool": f.tool,
                    }
                    for f in all_findings
                ],
            },
            confidence=0.9 if cppcheck_status == "ran" else 0.7,
        )
    ]

    findings_text = [
        f"line {f.line}: [{f.severity}/{f.tool}] {f.message}" for f in all_findings
    ]

    return build_result(
        metric=ctx.metric,
        analyzer_id=ANALYZER_ID,
        status="completed",
        score=score,
        measurements={
            "finding_count": len(all_findings),
            "builtin_count": len(builtin),
            "cppcheck_count": len(cppcheck_findings),
            "cppcheck_status": cppcheck_status,
            "cppcheck_state": cppcheck_state,
            "cppcheck_detail": cppcheck_detail,
            "checks_run": checks_run,
            "total_penalty": round(total_penalty, 4),
        },
        evidence_refs=evidence_refs,
        findings=findings_text,
        limitations=limitations,
    )
