"""Ollama model gateway — the ONLY path to LLM inference in this project.

Hard invariants:
- Loopback only: any base URL that does not resolve to 127.0.0.1/::1 or the
  literal hostnames ``localhost``/``127.0.0.1``/``::1`` is rejected at
  construction. There is no way to point the gateway at a remote host.
- No API keys: the Ollama protocol has no key concept. The client asserts
  that no ``Authorization`` header is ever attached to a request; if one
  ever appears (e.g. via a misconfigured transport), the call fails loudly
  instead of leaking credentials to a local service.
- No silent model substitution: the caller names the model; a missing model
  is an explicit error naming ``ollama pull <model>``.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from ..core.schemas import GenerationRecord
from .prompts import PromptError, load_prompt, prompt_hash

T = TypeVar("T", bound=BaseModel)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_API_GENERATE = "/api/generate"
_API_TAGS = "/api/tags"


class OllamaError(RuntimeError):
    """Base for all gateway failures."""


class OllamaUnreachable(OllamaError):
    """The local Ollama server could not be contacted."""


class OllamaModelMissing(OllamaError):
    """The requested model is not installed locally."""


class OllamaBadResponse(OllamaError):
    """The server replied, but the payload was unusable after retries."""


def _is_loopback(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return False
    return host.lower() in _LOOPBACK_HOSTS


class OllamaClient:
    """Thin, policy-enforcing client for a local Ollama server."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        timeout_s: float = 120.0,
        max_retries: int = 2,
        backoff_s: float = 1.0,
    ) -> None:
        if not _is_loopback(base_url):
            raise OllamaError(
                f"refusing non-loopback Ollama URL {base_url!r}: "
                "the gateway only talks to a local Ollama server"
            )
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._transport = httpx.Client(timeout=timeout_s, trust_env=False)

    # -- low-level ---------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """POST with bounded retries on transport/5xx errors. No auth headers."""
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                req = self._transport.build_request(
                    "POST", self.base_url + path, json=payload, timeout=timeout
                )
                # Invariant: no credentials may ever leave this process.
                assert "authorization" not in {k.lower() for k in req.headers}, (
                    "refusing to send an Authorization header to Ollama"
                )
                resp = self._transport.send(req)
                if resp.status_code >= 500:
                    raise OllamaError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code == 404 and "model" in resp.text.lower():
                    raise OllamaModelMissing(
                        f"Ollama has no such model: {payload.get('model')!r}. "
                        f"Run: ollama pull {payload.get('model')}"
                    )
                resp.raise_for_status()
                try:
                    return resp.json()  # type: ignore[no-any-return]
                except ValueError as exc:
                    raise OllamaBadResponse(
                        f"Ollama returned non-JSON: {resp.text[:200]!r}"
                    ) from exc
            except (httpx.TransportError, httpx.TimeoutException, OllamaError) as exc:
                last = exc
                if isinstance(exc, (OllamaModelMissing, OllamaBadResponse)):
                    break  # not retryable
                if attempt < self.max_retries:
                    time.sleep(self.backoff_s * (2**attempt))
        assert last is not None
        if isinstance(last, (httpx.TransportError, httpx.TimeoutException)):
            raise OllamaUnreachable(
                f"cannot reach Ollama at {self.base_url} ({last}). "
                "Is `ollama serve` running?"
            ) from last
        raise last

    def tags(self) -> list[str]:
        """List locally installed model names (GET /api/tags)."""
        try:
            resp = self._transport.get(self.base_url + _API_TAGS, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise OllamaUnreachable(
                f"cannot reach Ollama at {self.base_url} ({exc})"
            ) from exc
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]

    def generate(
        self,
        prompt: str,
        model: str,
        temperature: float = 0.0,
        seed: int | None = 42,
        timeout_s: float | None = None,
    ) -> str:
        """Raw text generation. Prefer :meth:`generate_structured`."""
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if seed is not None:
            payload["options"]["seed"] = seed
        body = self._post(
            _API_GENERATE, payload, timeout_s if timeout_s is not None else self.timeout_s
        )
        text = body.get("response", "")
        if not isinstance(text, str) or not text:
            raise OllamaBadResponse("Ollama returned an empty response")
        return text

    # -- structured --------------------------------------------------------
    def generate_structured(
        self,
        task: str,
        prompt: str,
        schema: type[T],
        model_cfg: dict[str, Any],
        prompt_name: str | None = None,
        validation_retries: int = 2,
    ) -> tuple[T, GenerationRecord]:
        """Generate text, extract JSON, validate against a Pydantic schema.

        ``model_cfg`` carries ``model`` (required), ``temperature``,
        ``seed``, ``timeout_s`` (from ``configs/models.yaml``).

        On JSON-parse or schema-validation failure the model is asked again
        (up to ``validation_retries`` extra attempts) with a short repair
        instruction appended. Returns the validated instance plus a
        :class:`GenerationRecord` for provenance.
        """
        model = str(model_cfg["model"])
        temperature = float(model_cfg.get("temperature", 0.0))
        seed = model_cfg.get("seed", 42)
        seed = int(seed) if seed is not None else None
        timeout_s = float(model_cfg.get("timeout_s", self.timeout_s))
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        p_hash = prompt_hash(prompt)
        p_version = "unknown"
        if prompt_name:
            try:
                fm, _ = load_prompt(prompt_name)
                p_version = str(fm.get("version", "unknown"))
            except PromptError:
                pass

        last_err: Exception | None = None
        current = prompt
        for attempt in range(validation_retries + 1):
            try:
                raw = self.generate(
                    current,
                    model=model,
                    temperature=temperature,
                    seed=seed,
                    timeout_s=timeout_s,
                )
            except OllamaBadResponse as exc:
                # The model replied, but with unusable content (e.g. non-JSON).
                # Treat as a failed generation attempt, not a transport error.
                last_err = exc
            else:
                try:
                    instance = schema.model_validate_json(_extract_json(raw))
                    record = GenerationRecord(
                        task=task,
                        model=model,
                        temperature=temperature,
                        seed=seed,
                        timeout_s=timeout_s,
                        prompt_version=p_version,
                        prompt_hash=p_hash,
                        started_at=started,
                        duration_s=time.monotonic() - t0,
                    )
                    return instance, record
                except (ValueError, ValidationError) as exc:
                    last_err = exc
            current = (
                prompt
                + "\n\nYour previous reply was not valid. It must be strict JSON "
                f"matching this schema: {schema.__name__}. Error: {last_err}. "
                "Reply with ONLY the corrected JSON, no prose."
            )
        raise OllamaBadResponse(
            f"model {model!r} failed to produce valid {schema.__name__} "
            f"after {validation_retries + 1} attempts: {last_err}"
        )

    def close(self) -> None:
        self._transport.close()


def _extract_json(text: str) -> str:
    """Lenient JSON extraction: strict parse first, else first {...}/[...] block."""
    text = text.strip()
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    for pattern in (r"\[.*\]", r"\{.*\}"):
        m = re.search(pattern, text, re.DOTALL)
        if m:
            candidate = m.group(0)
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON object or array found in model reply")
