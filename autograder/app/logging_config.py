"""Terminal logging setup — the pipeline's voice on stderr.

One configuration entry point for both entry points (the API server in
``api.main`` and the CLI runner in ``orchestration.run``). Everything the
pipeline does — workflow steps, LLM calls, analyzer runs, sandbox
readiness — is reported through stdlib ``logging`` loggers under the
``autograder.*`` namespace, so an operator watching the terminal sees
what is happening and how long each step takes, instead of only
uvicorn's access log.

Level: the ``level`` argument wins; otherwise the ``AUTOGRADER_LOG_LEVEL``
environment variable; otherwise ``INFO``. ``DEBUG`` additionally enables
prompt/response previews in the LLM gateway logs.
"""

from __future__ import annotations

import logging
import os
import sys

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


def configure_logging(level: str | None = None) -> int:
    """Configure root logging to stderr. Idempotent; returns the level.

    Safe to call more than once: the marked handler is installed at most
    once, and later calls only adjust the root level.
    """
    raw = level or os.environ.get("AUTOGRADER_LOG_LEVEL") or "INFO"
    resolved = _LEVELS.get(str(raw).strip().upper(), logging.INFO)
    root = logging.getLogger()
    if not any(getattr(h, "_autograder_handler", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        handler._autograder_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(resolved)
    return resolved
