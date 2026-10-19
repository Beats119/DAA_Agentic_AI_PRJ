"""Shared fixtures for integration tests.

The mock Ollama server below is CLEARLY labeled test-only infrastructure.
It never leaves ``tests/`` and is never consulted by production code paths.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class MockOllamaHandler(BaseHTTPRequestHandler):
    """Scripted fake Ollama. Configure per-test via ``server.script``.

    ``script`` maps endpoint -> list of actions, consumed in order:
      ("json", obj)      -> 200 with JSON body
      ("raw", text)      -> 200 with raw text body
      ("status", code)   -> bare response with that status
      ("status_body", code, text) -> response with status and body
      ("hang",)          -> sleep longer than any test timeout (then 200)
    Unscripted endpoints -> 404.
    """

    def _next(self, endpoint):
        script = self.server.script.get(endpoint, [])
        if not script:
            return ("status", 404)
        return script.pop(0)

    def _run(self, endpoint):
        action = self._next(endpoint)
        kind = action[0]
        if kind == "hang":
            import time

            time.sleep(30)
            body, ctype, code = b"{}", "application/json", 200
        elif kind == "json":
            body, ctype, code = json.dumps(action[1]).encode(), "application/json", 200
        elif kind == "raw":
            body, ctype, code = action[1].encode(), "text/plain", 200
        elif kind == "status_body":
            _, code, text = action
            body, ctype = text.encode(), "application/json"
        else:
            body, ctype, code = b"", "text/plain", action[1]
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._run("tags")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        self.server.last_request_body = self.rfile.read(length) if length else b""
        if self.path == "/api/generate":
            self._run("generate")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def mock_ollama():
    """Yield ``(base_url, server)`` for a scripted mock Ollama on loopback."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockOllamaHandler)
    server.script = {}
    server.last_request_body = b""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        thread.join(timeout=5)
