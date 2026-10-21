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

Inference policy (why generation looks the way it does):

- Generation is STREAMED (``"stream": true``, NDJSON chunks assembled by
  the client). Local inference on partially CPU-offloaded hardware runs
  at only a few tokens/second, so a *total* request deadline kills slow
  but healthy generations; an *idle* deadline does not.
- A task's ``timeout_s`` is therefore an IDLE timeout: the maximum
  seconds allowed with no streamed chunk arriving (the httpx read
  timeout), not a total-duration cap. A slow-but-progressing generation
  is never killed for taking long. The connect timeout is 10s.
- Retries are deliberately narrow: ONLY connect-class failures
  (``httpx.ConnectError`` / ``httpx.ConnectTimeout`` — the server is not
  up yet and no inference happened) are retried, at most
  ``connect_retries`` times. Every other failure raises immediately:
  repeating an expensive generation cannot fix a slow model, a server
  error, or a malformed reply, and historically burned tens of minutes
  per run doing exactly that.
- Requests carry ``keep_alive`` so the model stays loaded between the
  run's calls (a cold load costs ~90s and would otherwise land inside
  the first task's window), and ``num_ctx`` so the context window is an
  explicit, configured value rather than the server default.
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

# TCP connect is either fast or the server is not up; 10s is generous.
_CONNECT_TIMEOUT_S = 10.0
# Default context window (prompt + output tokens) sent as ``num_ctx``.
_DEFAULT_NUM_CTX = 8192
# Keep the model resident between a run's calls; reloads cost ~90s.
_DEFAULT_KEEP_ALIVE = "30m"
# Warm-up gets a long idle window: it exists to absorb the cold load.
_WARMUP_IDLE_TIMEOUT_S = 300.0


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
        connect_retries: int = 1,
        backoff_s: float = 1.0,
    ) -> None:
        """Create a client for a local Ollama server.

        ``timeout_s`` is the default IDLE timeout for generation (see the
        module docstring): the longest gap allowed between streamed
        chunks, not a total deadline. ``connect_retries`` is the number
        of extra attempts allowed for connect-class failures ONLY
        (server not up yet); ``backoff_s`` is the fixed pause before
        such a retry.
        """
        if not _is_loopback(base_url):
            raise OllamaError(
                f"refusing non-loopback Ollama URL {base_url!r}: "
                "the gateway only talks to a local Ollama server"
            )
        if connect_retries < 0:
            raise ValueError("connect_retries must be >= 0")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.connect_retries = connect_retries
        self.backoff_s = backoff_s
        self._transport = httpx.Client(timeout=timeout_s, trust_env=False)

    # -- low-level ---------------------------------------------------------
    @staticmethod
    def _consume_stream(
        resp: httpx.Response, t0: float
    ) -> tuple[dict[str, Any], float]:
        """Assemble one NDJSON generate stream.

        Returns ``(body, ttft_s)``: every chunk's ``response`` text
        concatenated, the last chunk's fields as the body (the final
        ``done`` chunk carries the run statistics: ``eval_count``,
        ``eval_duration``, ``load_duration``, ...), and the
        client-measured seconds from request start (``t0``) to the
        first chunk. A malformed line or an explicit ``error`` chunk is
        an :class:`OllamaBadResponse` — the server answered, but not
        with a usable generation.
        """
        parts: list[str] = []
        body: dict[str, Any] = {}
        ttft: float | None = None
        for line in resp.iter_lines():
            if not line.strip():
                continue
            if ttft is None:
                ttft = time.monotonic() - t0
            try:
                chunk = json.loads(line)
            except ValueError as exc:
                raise OllamaBadResponse(
                    f"Ollama returned a non-JSON stream chunk: {line[:200]!r}"
                ) from exc
            if not isinstance(chunk, dict) or "error" in chunk:
                detail = chunk.get("error") if isinstance(chunk, dict) else chunk
                raise OllamaBadResponse(f"Ollama stream error: {detail}")
            piece = chunk.get("response")
            if isinstance(piece, str):
                parts.append(piece)
            body = chunk
        assembled = dict(body)
        assembled["response"] = "".join(parts)
        if ttft is None:
            ttft = time.monotonic() - t0
        return assembled, ttft

    def _post_stream(
        self, path: str, payload: dict[str, Any], idle_timeout_s: float
    ) -> tuple[dict[str, Any], float]:
        """One logical streaming POST. Returns ``(body, ttft_s)``.

        The httpx read timeout is ``idle_timeout_s`` — the maximum gap
        between chunks — while the connect timeout is
        ``_CONNECT_TIMEOUT_S``. Retry policy: ONLY connect-class
        failures are retried (at most ``connect_retries`` extra
        attempts, fixed ``backoff_s`` pause); every other failure —
        idle/read timeout, HTTP error, missing model, malformed
        stream — raises immediately (see the module docstring).

        Error mapping (unchanged from the pre-streaming client):
        transport failures -> :class:`OllamaUnreachable`; HTTP >= 500
        -> :class:`OllamaError`; 404 naming a model ->
        :class:`OllamaModelMissing`; other 4xx -> the raw
        ``httpx.HTTPStatusError`` propagates; unusable body ->
        :class:`OllamaBadResponse`.

        Instrumentation: exactly one INFO line per HTTP attempt on the
        ``autograder.llm`` logger when the attempt concludes — attempt
        number, applicable maximum, path, outcome, elapsed seconds;
        never payload content. The maximum shown is the connect-class
        maximum while the sequence is in connect-retry territory, and
        the attempts actually made for single-attempt outcomes.
        """
        connect_total = self.connect_retries + 1
        denom = 1
        last: Exception | None = None
        for attempt in range(connect_total):
            n = attempt + 1
            t0 = time.monotonic()
            status: int | None = None
            try:
                req = self._transport.build_request(
                    "POST", self.base_url + path, json=payload,
                    timeout=httpx.Timeout(idle_timeout_s,
                                          connect=_CONNECT_TIMEOUT_S),
                )
                # Invariant: no credentials may ever leave this process.
                assert "authorization" not in {k.lower() for k in req.headers}, (
                    "refusing to send an Authorization header to Ollama"
                )
                resp = self._transport.send(req, stream=True)
                try:
                    status = resp.status_code
                    if status >= 400:
                        text = resp.read().decode("utf-8", "replace")
                        if status >= 500:
                            raise OllamaError(
                                f"Ollama HTTP {status}: {text[:200]}")
                        if status == 404 and "model" in text.lower():
                            raise OllamaModelMissing(
                                f"Ollama has no such model: "
                                f"{payload.get('model')!r}. "
                                f"Run: ollama pull {payload.get('model')}"
                            )
                        resp.raise_for_status()
                    body, ttft = self._consume_stream(resp, t0)
                finally:
                    resp.close()
                log.info("http attempt %d/%d POST %s -> ok (HTTP %d) in %.1fs",
                         n, max(denom, n), path, status,
                         time.monotonic() - t0)
                return body, ttft
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # The ONLY retryable class: no request reached the
                # server, so no inference is duplicated by retrying.
                denom = connect_total
                log.info("http attempt %d/%d POST %s -> %s after %.1fs",
                         n, max(denom, n), path, type(exc).__name__,
                         time.monotonic() - t0)
                last = exc
                if attempt < self.connect_retries:
                    time.sleep(self.backoff_s)
            except httpx.HTTPStatusError as exc:
                # Non-model 4xx from raise_for_status: never retried and
                # propagates as before — but the attempt still logs.
                log.info("http attempt %d/%d POST %s -> HTTP %d after %.1fs",
                         n, max(denom, n), path,
                         exc.response.status_code, time.monotonic() - t0)
                raise
            except (httpx.TransportError, OllamaError) as exc:
                # Everything else is terminal for this call: an idle
                # timeout mid-generation, a dropped connection, a 5xx,
                # a missing model, a malformed stream. Retrying would
                # repeat an expensive generation that cannot benefit.
                if isinstance(exc, OllamaError) and status is not None \
                        and status >= 500:
                    outcome = f"HTTP {status}"
                else:
                    outcome = type(exc).__name__
                log.info("http attempt %d/%d POST %s -> %s after %.1fs",
                         n, max(denom, n), path, outcome,
                         time.monotonic() - t0)
                if isinstance(exc, httpx.TransportError):
                    raise OllamaUnreachable(
                        f"cannot reach Ollama at {self.base_url} ({exc}). "
                        "Is `ollama serve` running?"
                    ) from exc
                raise
        assert last is not None
        raise OllamaUnreachable(
            f"cannot reach Ollama at {self.base_url} ({last}). "
            "Is `ollama serve` running?"
        ) from last

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

    def warmup(self, model: str = "llama3.1") -> float:
        """Load ``model`` into memory and pin it with ``keep_alive``.

        One minimal streaming generation (prompt ``"OK"``, 1 output
        token) through the normal logged path, with a generous 300s
        idle timeout so a cold model load (~90s observed on the target
        hardware) completes here instead of consuming the first real
        task's window. Returns the elapsed seconds. Raises whatever
        the generation raises — callers that treat warm-up as
        best-effort must catch it themselves.
        """
        t0 = time.monotonic()
        self._generate_full(
            "OK",
            model=model,
            temperature=0.0,
            seed=42,
            timeout_s=_WARMUP_IDLE_TIMEOUT_S,
            num_predict=1,
            num_ctx=_DEFAULT_NUM_CTX,
            keep_alive=_DEFAULT_KEEP_ALIVE,
            task="warmup",
        )
        return time.monotonic() - t0

    def generate(
        self,
        prompt: str,
        model: str,
        temperature: float = 0.0,
        seed: int | None = 42,
        timeout_s: float | None = None,
        num_predict: int | None = None,
        num_ctx: int = _DEFAULT_NUM_CTX,
        keep_alive: str = _DEFAULT_KEEP_ALIVE,
    ) -> str:
        """Raw text generation. Prefer :meth:`generate_structured`."""
        text, _usage = self._generate_full(
            prompt,
            model=model,
            temperature=temperature,
            seed=seed,
            timeout_s=timeout_s,
            num_predict=num_predict,
            num_ctx=num_ctx,
            keep_alive=keep_alive,
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
        num_ctx: int = _DEFAULT_NUM_CTX,
        keep_alive: str = _DEFAULT_KEEP_ALIVE,
        task: str = "generate",
    ) -> tuple[str, int]:
        """Generate once; return ``(text, tokens_used)``.

        The request is STREAMED: ``timeout_s`` is an idle timeout (the
        maximum gap between chunks), never a total-duration cap.
        ``num_predict`` is forwarded to Ollama as the output-length cap —
        the configured ``max_tokens`` is actually enforced (D27), not just
        recorded. ``num_ctx`` sizes the context window (prompt + output).
        ``tokens_used`` is Ollama's reported
        ``prompt_eval_count + eval_count`` when the server provides both;
        otherwise the documented estimate ``len//4`` per side.

        Every call is logged (logger ``autograder.llm``): one line when
        the request is sent and one when the response lands (or fails),
        with wall time, the client-measured time-to-first-chunk, and
        Ollama's own timing fields when provided.
        """
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "keep_alive": keep_alive,
            "options": {"temperature": temperature, "num_ctx": int(num_ctx)},
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
            body, ttft = self._post_stream(
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
        extras = f" ttft={ttft:.1f}s"
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
        ``seed``, ``timeout_s``, ``max_tokens`` and ``num_ctx`` (from
        ``configs/models.yaml``). ``max_tokens`` is sent to Ollama as
        ``num_predict`` so the configured output limit is enforced (D27);
        ``num_ctx`` sizes the context window (default 8192).

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
        num_ctx = int(model_cfg.get("num_ctx", _DEFAULT_NUM_CTX))
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
                    num_ctx=num_ctx,
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
