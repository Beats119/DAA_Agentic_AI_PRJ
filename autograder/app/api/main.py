"""Launch the AutoGrader web app.

Usage:
    python -m autograder.app.api.main [--host 127.0.0.1] [--port 8000]

Reads ``.env`` from the current directory when present (KEY=VALUE lines;
no interpolation). Relevant keys: ``OLLAMA_URL``, ``ARTIFACT_DIR``.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn


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
    parser = argparse.ArgumentParser(description="AutoGrader web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    from .app import create_app

    base_dir = Path(os.environ.get("ARTIFACT_DIR", Path.cwd() / "artifacts"))
    app = create_app(base_dir=base_dir, gateway_factory=_gateway_factory)
    print(f"AutoGrader on http://{args.host}:{args.port} "
          f"(data dir: {base_dir})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
