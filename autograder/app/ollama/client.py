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
import logging
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

log = logging.getLogger("autograder.llm")


def _preview(text: str, limit: int = 400) -> str:
    """One-line, length-capped preview for DEBUG logs (whitespace folded)."""
    return " ".join(text.split())[:limit]

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
        """POST with bounded retries on transport/5xx errors. No auth headers.

        Instrumentation: exactly one INFO line per attempt is logged on
        the ``autograder.llm`` logger when the attempt concludes —
        attempt number, path, outcome, and elapsed seconds only; never
        payload or prompt content.
        """
        last: Exception | None = None
        total = self.max_retries + 1
        for attempt in range(total):
            t0 = time.monotonic()
            status: int | None = None
            try:
                req = self._transport.build_request(
                    "POST", self.base_url + path, json=payload, timeout=timeout
                )
                # Invariant: no credentials may ever leave this process.
                assert "authorization" not in {k.lower() for k in req.headers}, (
                    "refusing to send an Authorization header to Ollama"
                )
                resp = self._transport.send(req)
                status = resp.status_code
                if resp.status_code >= 500:
                    raise OllamaError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code == 404 and "model" in resp.text.lower():
                    raise OllamaModelMissing(
                        f"Ollama has no such model: {payload.get('model')!r}. "
                        f"Run: ollama pull {payload.get('model')}"
                    )
                resp.raise_for_status()
                try:
                    body: dict[str, Any] = resp.json()
                except ValueError as exc:
                    raise OllamaBadResponse(
                        f"Ollama returned non-JSON: {resp.text[:200]!r}"
                    ) from exc
                log.info("http attempt %d/%d POST %s -> ok (HTTP %d) in %.1fs",
                         attempt + 1, total, path, resp.status_code,
                         time.monotonic() - t0)
                return body
            except httpx.HTTPStatusError as exc:
                # Non-model 4xx from raise_for_status: never retried and
                # propagates as before — but the attempt still logs.
                log.info("http attempt %d/%d POST %s -> HTTP %d after %.1fs",
                         attempt + 1, total, path,
                         exc.response.status_code, time.monotonic() - t0)
                raise
            except (httpx.TransportError, httpx.TimeoutException, OllamaError) as exc:
                if isinstance(exc, (httpx.TransportError, httpx.TimeoutException,
                                    OllamaBadResponse)):
                    outcome = type(exc).__name__
                elif status is not None:
                    outcome = f"HTTP {status}"
                else:
                    outcome = type(exc).__name__
                log.info("http attempt %d/%d POST %s -> %s after %.1fs",
                         attempt + 1, total, path, outcome,
                         time.monotonic() - t0)
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
        num_predict: int | None = None,
    ) -> str:
        """Raw text generation. Prefer :meth:`generate_structured`."""
        text, _usage = self._generate_full(
            prompt,
            model=model,
            temperature=temperature,
            seed=seed,
            timeout_s=timeout_s,
            num_predict=num_predict,
        )
        return text

    def _generate_full(
        self,
        prompt: str,
        model: str,
        temperature: float = 0.0,
        seed: int | None = 42,
        timeout_s: float | None = None,
        num_predict: int | None = None,
        task: str = "generate",
    ) -> tuple[str, int]:
        """Generate once; return ``(text, tokens_used)``.

        ``num_predict`` is forwarded to Ollama as the output-length cap —
        the configured ``max_tokens`` is actually enforced (D27), not just
        recorded. ``tokens_used`` is Ollama's reported
        ``prompt_eval_count + eval_count`` when the server provides both;
        otherwise the documented estimate ``len//4`` per side.

        Every call is logged (logger ``autograder.llm``): one line when
        the request is sent and one when the response lands (or fails),
        with wall time and Ollama's own timing fields when provided.
        """
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if seed is not None:
            payload["options"]["seed"] = seed
        if num_predict is not None:
            payload["options"]["num_predict"] = int(num_predict)
        log.info("→ task=%s model=%s prompt=%d chars", task, model, len(prompt))
        if log.isEnabledFor(logging.DEBUG):
            log.debug("→ task=%s prompt preview: %s", task, _preview(prompt))
        t0 = time.monotonic()
        try:
            body = self._post(
                _API_GENERATE, payload,
                timeout_s if timeout_s is not None else self.timeout_s
            )
        except OllamaError as exc:
            log.warning("← task=%s model=%s failed after %.1fs: %s",
                        task, model, time.monotonic() - t0, exc)
            raise
        elapsed = time.monotonic() - t0
        text = body.get("response", "")
        if not isinstance(text, str) or not text:
            log.warning("← task=%s model=%s failed after %.1fs: "
                        "empty response from Ollama", task, model, elapsed)
            raise OllamaBadResponse("Ollama returned an empty response")
        eval_count = body.get("eval_count")
        prompt_eval_count = body.get("prompt_eval_count")
        if isinstance(eval_count, int) and isinstance(prompt_eval_count, int):
            tokens_used = eval_count + prompt_eval_count
        else:
            tokens_used = len(prompt) // 4 + len(text) // 4
        extras = ""
        if isinstance(eval_count, int):
            extras += f" tokens={eval_count}"
        eval_duration = body.get("eval_duration")
        if isinstance(eval_duration, (int, float)):
            extras += f" gen={eval_duration / 1e9:.1f}s"
        total_duration = body.get("total_duration")
        if isinstance(total_duration, (int, float)):
            extras += f" total={total_duration / 1e9:.1f}s"
        log.info("← task=%s model=%s %.1fs response=%d chars%s",
                 task, model, elapsed, len(text), extras)
        if log.isEnabledFor(logging.DEBUG):
            log.debug("← task=%s response preview: %s", task, _preview(text))
        return text, tokens_used

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
        ``seed``, ``timeout_s`` and ``max_tokens`` (from
        ``configs/models.yaml``). ``max_tokens`` is sent to Ollama as
        ``num_predict`` so the configured output limit is enforced (D27).

        On JSON-parse or schema-validation failure the model is asked again
        (up to ``validation_retries`` extra attempts) with a short repair
        instruction appended. Returns the validated instance plus a
        :class:`GenerationRecord` for provenance; the record's
        ``tokens_used`` accumulates usage across *all* attempts (D10).
        """
        model = str(model_cfg["model"])
        temperature = float(model_cfg.get("temperature", 0.0))
        seed = model_cfg.get("seed", 42)
        seed = int(seed) if seed is not None else None
        timeout_s = float(model_cfg.get("timeout_s", self.timeout_s))
        max_tokens = model_cfg.get("max_tokens")
        num_predict = int(max_tokens) if max_tokens else None
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
        tokens_total = 0
        for attempt in range(validation_retries + 1):
            try:
                raw, usage = self._generate_full(
                    current,
                    model=model,
                    temperature=temperature,
                    seed=seed,
                    timeout_s=timeout_s,
                    num_predict=num_predict,
                    task=task,
                )
                tokens_total += usage
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
                        tokens_used=tokens_total,
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
