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
from .checks import SEVERITY_PENALTY, StaticFinding, run_builtin_checks

ANALYZER_ID = "static_analysis"

DEFAULTS = {
    "score_floor": 0.0,
    "severity_penalty": dict(SEVERITY_PENALTY),
    "use_cppcheck": True,
    "cppcheck_timeout_s": 30,
}


def _run_cppcheck(source_path: str, timeout_s: float, executor: Any) -> list[StaticFinding] | None:
    """Run cppcheck if installed, inside the sandbox.

    Returns None when cppcheck is unavailable, no executor was provided
    (cppcheck is never run unsandboxed), or the sandboxed run fails /
    times out (not an analyzer error — the built-in checks still ran).
    """
    exe = shutil.which("cppcheck")
    if exe is None or executor is None:
        return None
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
    except Exception:
        return None
    if result.timed_out:
        return None
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
    return findings


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
    cppcheck_status = "not_attempted"
    if params["use_cppcheck"]:
        result = _run_cppcheck(ctx.source_path, float(params["cppcheck_timeout_s"]), ctx.executor)
        if result is None:
            cppcheck_status = "unavailable"
        else:
            cppcheck_status = "ran"
            cppcheck_findings = result

    all_findings = builtin + cppcheck_findings
    limitations: list[str] = []
    if cppcheck_status == "unavailable":
        limitations.append(
            "cppcheck is not installed on this host — only built-in checks ran "
            "(install cppcheck for deeper analysis)"
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
                },
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
            "total_penalty": round(total_penalty, 4),
        },
        evidence_refs=evidence_refs,
        findings=findings_text,
        limitations=limitations,
    )
