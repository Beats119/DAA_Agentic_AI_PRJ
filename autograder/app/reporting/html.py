"""HTML report rendering.

Read-only view over a validated :class:`Report`: this module never mutates
the grade, the evidence, or the report object — it only renders what the
deterministic pipeline produced. Jinja2 autoescape is always on; student
source and every free-text field are escaped. Server filesystem paths must
already be sanitized by the workflow (``<run_dir>``); this renderer adds a
belt-and-braces redaction of anything that still looks like an absolute
server path.
"""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..core.schemas import Report

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# Absolute POSIX paths that slipped through sanitization (e.g. /home/...,
# /tmp/..., /var/...). The workflow replaces the run dir with <run_dir>;
# this catches anything else before it reaches the browser.
_PATH_RE = re.compile(r"(/(?:home|root|tmp|var|etc|opt|srv|mnt|usr|private)[^\s\"'<>]*)")


def _redact_paths(text: str) -> str:
    return _PATH_RE.sub("<redacted-path>", text)


def _short_hash(value: str | None) -> str:
    if not value:
        return "—"
    digest = value[7:] if value.startswith("sha256:") else value
    return digest[:12] + "…" if len(digest) > 12 else digest


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["short_hash"] = _short_hash
    env.filters["redact_paths"] = _redact_paths
    return env


def render_report_html(report: Report, source_code: str | None = None) -> str:
    """Render ``report`` to an HTML fragment (a ``<section>``).

    ``source_code`` is the student's C source, shown escaped in a final
    section when provided. Totals shown reconcile by construction: the
    fragment displays grade and per-metric contributions exactly as the
    validated report carries them.
    """
    template = _env().get_template("report_fragment.html")
    metrics = list(report.metrics or [])
    contrib_total = sum(m.contribution for m in metrics)
    return template.render(
        report=report,
        source_code=source_code,
        contrib_total=contrib_total,
    )


__all__ = ["render_report_html"]
