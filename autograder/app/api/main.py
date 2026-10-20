"""Launch the AutoGrader web app.

Usage:
    python -m autograder.app.api.main [--host 127.0.0.1] [--port 8000]

Reads ``.env`` from the current directory when present (KEY=VALUE lines;
no interpolation). Relevant keys: ``OLLAMA_URL``, ``ARTIFACT_DIR``,
``AUTOGRADER_LOG_LEVEL``.

Pipeline progress (workflow steps, LLM calls, analyzer runs) is logged
to the terminal via stdlib logging; see ``logging_config``. Uvicorn's
per-request access log is OFF by default — the run page polls every
couple of seconds and would bury the real logs; pass ``--access-log``
to re-enable it.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from ..logging_config import configure_logging

logger = logging.getLogger("autograder.api")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _gateway_factory():
    """Build an OllamaClient when the local server is reachable, else None."""
    from ..ollama.client import OllamaClient
    from ..ollama.health import probe

    url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    try:
        status = probe(url)
    except Exception:
        return None
    if not status.get("reachable"):
        return None
    return OllamaClient(url)


def main() -> None:
    _load_dotenv(Path.cwd() / ".env")
    configure_logging()
    parser = argparse.ArgumentParser(description="AutoGrader web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--access-log", action="store_true",
                        help="enable uvicorn's per-request access log "
                             "(default off: the run page's polling floods "
                             "the terminal and buries the pipeline logs)")
    args = parser.parse_args()

    from .app import create_app

    base_dir = Path(os.environ.get("ARTIFACT_DIR", Path.cwd() / "artifacts"))
    app = create_app(base_dir=base_dir, gateway_factory=_gateway_factory)
    logger.info("AutoGrader on http://%s:%d (data dir: %s)",
                args.host, args.port, base_dir)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info",
                access_log=args.access_log)


if __name__ == "__main__":
    main()
