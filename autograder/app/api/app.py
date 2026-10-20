"""FastAPI application: REST API + server-rendered UI.

All routes are thin, read-only views over the deterministic pipeline:
nothing here mutates grades or evidence. Uploads are size-capped —
enforced incrementally at the ASGI layer (UploadGuardMiddleware) and
again while streaming to disk, so an oversized upload is rejected
before it is fully received — stored under server-generated ids, and
treated as untrusted content.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

logger = logging.getLogger("autograder.api")

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE.parent / "templates"
_STATIC_DIR = _HERE.parent / "static"

_MAX_PDF_BYTES = 20 * 1024 * 1024
_MAX_C_BYTES = 1 * 1024 * 1024
_MAX_RULES_BYTES = 512 * 1024

_UPLOAD_CHUNK = 64 * 1024
# Multipart framing (boundaries, headers) rides on top of the file bytes;
# the transport-level guard allows this much slack over the file cap
# before rejecting. The handler enforces the exact cap on file bytes.
_FRAMING_SLACK = 64 * 1024
_MAX_CONCURRENT_UPLOADS = 4

_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ARTIFACT_ALLOWLIST = {
    "report.json": "application/json",
    "report.html": "text/html",
    "evidence.json": "application/json",
    "manifest.json": "application/json",
    "source.c": "text/plain",
}


_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:/[\w.@+\-]+){2,}/?")


def _strip_paths(text: str) -> str:
    """Remove absolute host paths from error text (D19)."""
    return _ABS_PATH_RE.sub("<path>", text)


async def _send_json_error(send: Any, status: int, code: str,
                           message: str) -> None:
    """Send a structured ``{"error": {code, message}}`` JSON response."""
    payload = json.dumps(
        {"error": {"code": code, "message": message}}
    ).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


class UploadGuardMiddleware:
    """Pure-ASGI guard for the upload routes (D17).

    - Bounds concurrent uploads with a semaphore; excess requests get a
      structured 429 immediately, before any body is read.
    - Counts request-body bytes as they stream in and rejects with 413
      the moment the route's cap (plus multipart framing slack) is
      exceeded — an oversized upload is never fully received first.
      Accepted bodies are replayed to the application unchanged. The
      in-flight buffer is bounded by cap + slack + one transport chunk.
    """

    def __init__(self, app: Any, *, limits: dict[str, int],
                 max_concurrent: int = _MAX_CONCURRENT_UPLOADS) -> None:
        self.app = app
        self.limits = dict(limits)
        self._slots = threading.Semaphore(max_concurrent)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (scope.get("type") != "http"
                or scope.get("method") != "POST"
                or scope.get("path") not in self.limits):
            await self.app(scope, receive, send)
            return
        limit = self.limits[scope["path"]]
        if not self._slots.acquire(blocking=False):
            await _send_json_error(
                send, 429, "too_many_uploads",
                "too many concurrent uploads; retry shortly")
            return
        try:
            body = bytearray()
            cap = limit + _FRAMING_SLACK
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return  # client went away; nothing to answer
                if message["type"] != "http.request":
                    continue
                body += message.get("body", b"")
                if len(body) > cap:
                    await _send_json_error(
                        send, 413, "upload_too_large",
                        f"upload exceeds the {limit}-byte limit")
                    return
                if not message.get("more_body", False):
                    break
            payload = bytes(body)
            delivered = False

            async def _replay() -> dict[str, Any]:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": payload,
                            "more_body": False}
                return {"type": "http.disconnect"}

            await self.app(scope, _replay, send)
        finally:
            self._slots.release()


def _templates() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["short_hash"] = (
        lambda v: (v[7:19] + "…") if v and len(v) > 19 else (v or "—")
    )
    return env


class EvaluationRequest(BaseModel):
    assignment_id: str
    submission_id: str
    rules_id: str


def _seed_rules(rules_dir: Path) -> None:
    """Copy the bundled example rules into the user rules dir on first run."""
    if any(rules_dir.glob("*.json")) or any(rules_dir.glob("*.yaml")):
        return
    examples = Path(__file__).resolve().parent.parent.parent.parent / "examples" / "rules"
    for src in sorted(examples.glob("*.json")):
        shutil.copy2(src, rules_dir / src.name)


def _read_rules(rules_dir: Path) -> list[dict[str, Any]]:
    from ..rules.loader import load_rules

    out: list[dict[str, Any]] = []
    for path in sorted(rules_dir.glob("*.json")) + sorted(rules_dir.glob("*.yaml")):
        try:
            rules = load_rules(path)
            out.append({
                "id": path.stem,
                "name": rules.problem_id,
                "version": rules.version,
                "hash": "",
                "metrics": [m.name for m in rules.metrics],
                "path": str(path),
            })
        except Exception:
            continue
    for path in sorted(rules_dir.glob("*.yml")):
        try:
            rules = load_rules(path)
            out.append({"id": path.stem, "name": rules.problem_id,
                        "version": rules.version, "hash": "",
                        "metrics": [m.name for m in rules.metrics],
                        "path": str(path)})
        except Exception:
            continue
    return out


def _prompt_versions() -> list[dict[str, str]]:
    prompts_dir = Path(__file__).resolve().parent.parent.parent.parent / "prompts"
    out: list[dict[str, str]] = []
    for path in sorted(prompts_dir.glob("*.txt")):
        version = "unknown"
        try:
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines()[:12]:
                if line.lower().startswith("version:"):
                    version = line.split(":", 1)[1].strip().strip('"')
                    break
        except OSError:
            pass
        out.append({"name": path.name, "version": version})
    return out


def create_app(
    base_dir: str | Path | None = None,
    gateway_factory: Any | None = None,
    max_concurrent_uploads: int = _MAX_CONCURRENT_UPLOADS,
) -> FastAPI:
    """Build the FastAPI app rooted at ``base_dir``."""
    from ..ollama import health as ollama_health
    from ..persistence.store import RunStore
    from ..reporting.schemas import validate_report
    from ..rules.loader import load_rules
    from ..rules.versioning import hash_rules
    from ..sandbox.capabilities import probe_capabilities
    from ..scoring.engine import POLICY_VERSION

    base = Path(base_dir) if base_dir else Path.cwd() / "artifacts"
    uploads = base / "uploads"
    assignments_dir = uploads / "assignments"
    submissions_dir = uploads / "submissions"
    rules_dir = base / "rules"
    for d in (assignments_dir, submissions_dir, rules_dir):
        d.mkdir(parents=True, exist_ok=True)
    _seed_rules(rules_dir)

    meta_path = uploads / "meta.json"
    meta: dict[str, Any] = {"assignments": {}, "submissions": {}}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    _meta_lock = threading.RLock()

    def _save_meta() -> None:
        """Persist upload metadata atomically under the meta lock (D19):
        readers never see a half-written meta.json."""
        with _meta_lock:
            fd, tmp_name = tempfile.mkstemp(dir=str(uploads), prefix=".meta_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, indent=1)
                os.replace(tmp_name, meta_path)
            finally:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def _sanitize_error(text: str) -> str:
        """Error text safe for clients: no server paths (D19)."""
        return _strip_paths(text.replace(str(base), "<data-dir>"))

    store = RunStore(base)
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ag_eval")
    futures: dict[str, Future] = {}

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Startup recovery (D26): runs left non-terminal by a previous
        # process were interrupted by its exit — fail them honestly and
        # drop artifact files no DB row references.
        try:
            recovered = store.recover_interrupted_runs()
            orphans = store.reconcile_orphan_files()
            if recovered or orphans:
                logger.info(
                    "startup recovery: %d interrupted run(s) failed, "
                    "%d orphan artifact file(s) removed",
                    len(recovered), orphans)
        except Exception:
            logger.exception("startup recovery failed")
        yield
        pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="AutoGrader", version="0.1.0", lifespan=_lifespan)
    app.add_middleware(
        UploadGuardMiddleware,
        limits={
            "/api/assignments": _MAX_PDF_BYTES,
            "/api/submissions": _MAX_C_BYTES,
            "/api/rules": _MAX_RULES_BYTES,
        },
        max_concurrent=max_concurrent_uploads,
    )

    @app.exception_handler(Exception)
    async def _unhandled(request: Any, exc: Exception) -> JSONResponse:
        # Log the traceback server-side; clients get a structured error
        # with no paths, no traceback, no internals (D19).
        logger.exception("unhandled API error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error",
                               "message": "internal server error"}},
        )

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    tpl = _templates()

    def _ollama_status() -> dict[str, Any]:
        import os

        url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
        try:
            status = ollama_health.probe(url)
        except Exception as exc:  # never break the page on a probe failure
            status = {"reachable": False, "models": [], "recommended": [],
                      "error": f"probe failed: {exc}"}
        status["url"] = url
        return status

    def _sandbox_caps() -> dict[str, Any]:
        try:
            return probe_capabilities().model_dump(mode="json")
        except Exception as exc:
            return {"error": _sanitize_error(str(exc))}

    def _sandbox_ready(caps: dict[str, Any]) -> bool:
        """True iff every PROBED mandatory mechanism is available.

        The mandatory policy (sandbox/policy.py) requires resource
        limits, Landlock, a network namespace and the seccomp filter;
        ``no_new_privs`` is applied unconditionally by the launcher and
        has no separate probe. Anything less and evaluations fail
        closed, so the dashboard must not claim "ready".
        """
        return bool(
            caps.get("resource_limits")
            and caps.get("network_namespace")
            and caps.get("seccomp")
            and caps.get("landlock_abi") is not None
        )

    def _sandbox_rows(caps: dict[str, Any]) -> list[dict[str, Any]]:
        """Display rows for the operations page.

        Booleans render as yes/no badges; measured values (the Landlock
        ABI version, the compiler path) render as values — flattening
        an ABI version into "yes" would misstate the probe result.
        """
        rows: list[dict[str, Any]] = []
        for name, value in caps.items():
            if isinstance(value, bool):
                rows.append({"name": name, "kind": "bool", "value": value})
            elif value is None:
                rows.append({"name": name, "kind": "bool", "value": False})
            else:
                rows.append({"name": name, "kind": "value", "value": value})
        return rows

    def _versions() -> dict[str, str]:
        import sys

        from autograder import __version__ as app_version
        from ..sandbox.executor import SANDBOX_VERSION

        return {
            "scoring_policy": POLICY_VERSION,
            "sandbox": SANDBOX_VERSION,
            "python": sys.version.split()[0],
            "app": app_version,
        }

    def _run_state(run_id: str) -> dict[str, Any]:
        """Current status of a run, accounting for thread crashes."""
        from ..persistence.store import RunNotFoundError

        fut = futures.get(run_id)
        try:
            rec = store.get_run(run_id)
        except RunNotFoundError:
            # The worker thread has not created the record yet.
            if fut is not None and not fut.done():
                return {"run_id": run_id, "status": "queued", "grade": None}
            if fut is not None:
                try:
                    fut.result()
                except Exception as exc:
                    return {"run_id": run_id, "status": "failed",
                            "grade": None,
                            "error": "evaluation failed before start: "
                                     + _sanitize_error(str(exc))}
            raise HTTPException(404, f"unknown run {run_id}")
        state = rec.state
        if fut is not None and fut.done():
            try:
                fut.result()
            except Exception as exc:
                if state not in ("completed", "completed_with_warnings",
                                 "failed", "incomplete"):
                    state = "failed"
                    return {"run_id": run_id, "status": state,
                            "grade": rec.grade,
                            "error": "evaluation thread crashed: "
                                     + _sanitize_error(str(exc))}
        return {"run_id": run_id, "status": state, "grade": rec.grade}

    def _load_report(run_id: str) -> dict[str, Any]:
        path = base / "runs" / run_id / "report.json"
        if not path.exists():
            raise HTTPException(404, f"no report for run {run_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _render_report_page(run_id: str) -> str:
        from ..reporting.html import render_report_html

        report = validate_report(_load_report(run_id))
        src_path = base / "runs" / run_id / "source.c"
        source = (src_path.read_text(encoding="utf-8", errors="replace")
                  if src_path.exists() else None)
        fragment = render_report_html(report, source)
        return tpl.get_template("report_page.html").render(
            active="dash", run_id=run_id, report_html=fragment)

    # ---------------- HTML pages ----------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        runs = [asdict(r) for r in store.list_runs(limit=20)]
        caps = _sandbox_caps()
        return tpl.get_template("index.html").render(
            active="dash", runs=runs, ollama=_ollama_status(),
            sandbox_ok=_sandbox_ready(caps),
            scoring_policy=POLICY_VERSION)

    @app.get("/new", response_class=HTMLResponse)
    def new_evaluation() -> str:
        rules = _read_rules(rules_dir)
        return tpl.get_template("new_evaluation.html").render(
            active="new", rules=rules,
            assignments=[{"id": k, **v}
                         for k, v in meta["assignments"].items()],
            submissions=[{"id": k, **v}
                         for k, v in meta["submissions"].items()],
            ollama=_ollama_status())

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: str) -> str:
        if not _RUN_ID_RE.match(run_id):
            raise HTTPException(400, "invalid run id")
        state = _run_state(run_id)
        status = state["status"]
        terminal = status in ("completed", "completed_with_warnings",
                              "incomplete", "failed")
        report_html = None
        if terminal:
            try:
                from ..reporting.html import render_report_html

                report = validate_report(_load_report(run_id))
                src_path = base / "runs" / run_id / "source.c"
                source = (src_path.read_text(encoding="utf-8", errors="replace")
                          if src_path.exists() else None)
                report_html = render_report_html(report, source)
            except HTTPException:
                report_html = None
        error = state.get("error")
        if error is None and status == "failed" and report_html is None:
            # A failed run may carry its reason in the interrupted.txt
            # artifact (written by startup recovery) instead of a report.
            try:
                raw = store.read_artifact(run_id, "interrupted.txt")
                error = _sanitize_error(
                    raw.decode("utf-8", "replace").strip()) or None
            except Exception:
                error = None
        return tpl.get_template("run_detail.html").render(
            active="dash", run_id=run_id, status=status,
            report_html=report_html, terminal=terminal, error=error)

    @app.get("/rules", response_class=HTMLResponse)
    def rules_page() -> str:
        return tpl.get_template("rules.html").render(
            active="rules", rules=_read_rules(rules_dir))

    @app.get("/operations", response_class=HTMLResponse)
    def operations() -> str:
        from ..sandbox.policy import load_policy

        ollama = _ollama_status()
        caps = _sandbox_caps()
        try:
            mandatory = list(load_policy().mandatory)
            policy_error = None
        except Exception as exc:  # a broken policy must not break the page
            mandatory = []
            policy_error = _sanitize_error(str(exc))
        return tpl.get_template("operations.html").render(
            active="ops", ollama=ollama, ollama_url=ollama.get("url", ""),
            sandbox_rows=_sandbox_rows(caps), versions=_versions(),
            prompts=_prompt_versions(), mandatory=mandatory,
            policy_error=policy_error)

    # ---------------- REST API ----------------
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "ollama": _ollama_status(),
                "sandbox": _sandbox_caps(), "versions": _versions()}

    async def _stream_upload_to_temp(
        file: UploadFile, directory: Path, limit: int, too_large: str,
        suffix: str = "",
    ) -> tuple[Path, int, str]:
        """Stream an upload to a temp file in bounded chunks (D17).

        The size cap is enforced *while* streaming: as soon as the limit
        is exceeded the partial file is deleted and 413 is raised — the
        upload is never fully buffered in memory first. The temp file is
        removed on every failure path (oversize, disconnect, I/O error).
        Returns ``(tmp_path, size, sha256)``; the caller finalizes or
        deletes the temp file.
        """
        tmp = directory / f".tmp_{uuid.uuid4().hex}{suffix}"
        size = 0
        hasher = hashlib.sha256()
        try:
            with open(tmp, "wb") as fh:
                while True:
                    chunk = await file.read(_UPLOAD_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, too_large)
                    hasher.update(chunk)
                    fh.write(chunk)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return tmp, size, "sha256:" + hasher.hexdigest()

    def _finalize_upload(kind: str, directory: Path, tmp: Path, ext: str,
                         sha256: str, size: int) -> dict[str, Any]:
        """Move a validated temp upload to its server-generated name and
        record it in the upload metadata."""
        uid = uuid.uuid4().hex
        os.replace(tmp, directory / f"{uid}{ext}")
        entry: dict[str, Any] = {"id": uid, "sha256": sha256, "size": size}
        with _meta_lock:
            meta[kind][uid] = entry
            _save_meta()
        return entry

    def _validate_assignment(tmp: Path) -> str:
        """Return the extension for a streamed assignment upload.

        Accepts PDFs (by magic bytes) and plain-text assignments; text
        must decode as UTF-8 and contain real content (not just control
        bytes). JSON-looking text (first non-whitespace char ``{``) is
        accepted without the content check, as before.
        """
        with open(tmp, "rb") as fh:
            if fh.read(8).startswith(b"%PDF"):
                return ".pdf"
        decoder = codecs.getincrementaldecoder("utf-8")()
        alnum = 0
        first_non_ws: str | None = None
        try:
            with open(tmp, "rb") as fh:
                while True:
                    chunk = fh.read(_UPLOAD_CHUNK)
                    if not chunk:
                        break
                    text = decoder.decode(chunk)
                    if first_non_ws is None:
                        stripped = text.lstrip()
                        if stripped:
                            first_non_ws = stripped[0]
                    alnum += sum(1 for ch in text if ch.isalnum())
                decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            raise HTTPException(400, "not a PDF or text assignment")
        if first_non_ws == "{" or alnum >= 3:
            return ".txt"
        raise HTTPException(400, "not a PDF or text assignment")

    def _validate_submission(tmp: Path) -> None:
        """A submission must be non-empty UTF-8 text."""
        _WS = b" \t\n\r\x0b\x0c"
        decoder = codecs.getincrementaldecoder("utf-8")()
        has_content = False
        try:
            with open(tmp, "rb") as fh:
                while True:
                    chunk = fh.read(_UPLOAD_CHUNK)
                    if not chunk:
                        break
                    if not has_content and chunk.strip(_WS):
                        has_content = True
                    decoder.decode(chunk)
                decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            if not has_content:
                raise HTTPException(400, "empty submission")
            raise HTTPException(400, "submission is not UTF-8 text")
        if not has_content:
            raise HTTPException(400, "empty submission")

    @app.post("/api/assignments", status_code=201)
    async def upload_assignment(file: UploadFile = File(...)) -> dict[str, Any]:
        tmp, size, sha = await _stream_upload_to_temp(
            file, assignments_dir, _MAX_PDF_BYTES,
            "assignment too large (max 20 MB)")
        try:
            ext = _validate_assignment(tmp)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        entry = _finalize_upload("assignments", assignments_dir, tmp, ext,
                                 sha, size)
        pages = None
        if ext == ".pdf":
            try:
                from ..ingestion.pdf import ingest_assignment

                stored = assignments_dir / f"{entry['id']}{ext}"
                pages = len(ingest_assignment(str(stored)).pages)
            except Exception:
                pages = None
        return {**entry, "pages": pages}

    @app.post("/api/submissions", status_code=201)
    async def upload_submission(file: UploadFile = File(...)) -> dict[str, Any]:
        tmp, size, sha = await _stream_upload_to_temp(
            file, submissions_dir, _MAX_C_BYTES,
            "submission too large (max 1 MB)")
        try:
            _validate_submission(tmp)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return _finalize_upload("submissions", submissions_dir, tmp, ".c",
                                sha, size)

    @app.get("/api/rules")
    def list_rules() -> list[dict[str, Any]]:
        return [{k: v for k, v in r.items() if k != "path"}
                for r in _read_rules(rules_dir)]

    @app.post("/api/rules", status_code=201)
    async def upload_rules(file: UploadFile = File(...)) -> dict[str, Any]:
        # Preserve a real extension end to end: the rules loader dispatches
        # on it, so the stored file keeps the uploaded format's suffix —
        # a YAML upload stays a .yaml file that listing and evaluations can
        # actually load (D18).
        raw_name = (file.filename or "").lower()
        ext = ".yaml" if raw_name.endswith((".yaml", ".yml")) else ".json"
        tmp, _size, _sha = await _stream_upload_to_temp(
            file, rules_dir, _MAX_RULES_BYTES,
            "rules file too large (max 512 KB)", suffix=ext)
        uid = uuid.uuid4().hex
        try:
            try:
                rules = load_rules(tmp)
            except Exception as exc:
                raise HTTPException(
                    422, f"invalid rules: {_sanitize_error(str(exc))}")
            os.replace(tmp, rules_dir / f"{uid}{ext}")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        rh = hash_rules(rules)
        return {"id": uid, "version": rules.version,
                "hash": rh if rh.startswith("sha256:") else "sha256:" + rh,
                "metrics": [m.name for m in rules.metrics]}

    @app.post("/api/evaluations", status_code=202)
    def create_evaluation(req: EvaluationRequest) -> dict[str, Any]:
        a = meta["assignments"].get(req.assignment_id)
        s = meta["submissions"].get(req.submission_id)
        rules = next((r for r in _read_rules(rules_dir)
                      if r["id"] == req.rules_id), None)
        if a is None:
            raise HTTPException(404, "unknown assignment_id")
        if s is None:
            raise HTTPException(404, "unknown submission_id")
        if rules is None:
            raise HTTPException(404, "unknown rules_id")
        # Fail fast on invalid rules (the workflow would reject them too).
        try:
            load_rules(rules["path"])
        except Exception as exc:
            raise HTTPException(
                422, f"invalid rules: {_sanitize_error(str(exc))}")

        run_id = uuid.uuid4().hex
        assignment_path = next(
            assignments_dir.glob(f"{req.assignment_id}.*"), None)
        if assignment_path is None:
            raise HTTPException(404, "assignment file missing")
        submission_path = submissions_dir / f"{req.submission_id}.c"
        if not submission_path.is_file():
            raise HTTPException(404, "submission file missing")

        def _run() -> None:
            gateway = None
            if gateway_factory is not None:
                try:
                    gateway = gateway_factory()
                except Exception:
                    gateway = None
            from ..orchestration.workflow import evaluate

            # Share this app's RunStore with the workflow (D26): one
            # store instance per process per DB, so the store lock spans
            # request and evaluation threads.
            evaluate(str(assignment_path), str(submission_path),
                     rules["path"], gateway=gateway, run_id=run_id,
                     base_dir=base, store=store)

        futures[run_id] = pool.submit(_run)
        return {"run_id": run_id, "status": "queued"}

    @app.get("/api/evaluations/{run_id}")
    def evaluation_status(run_id: str) -> dict[str, Any]:
        if not _RUN_ID_RE.match(run_id):
            raise HTTPException(400, "invalid run id")
        return _run_state(run_id)

    @app.get("/api/evaluations/{run_id}/report")
    def evaluation_report(run_id: str) -> dict[str, Any]:
        if not _RUN_ID_RE.match(run_id):
            raise HTTPException(400, "invalid run id")
        report = validate_report(_load_report(run_id))
        return json.loads(report.model_dump_json())

    @app.get("/api/evaluations/{run_id}/report.html",
             response_class=HTMLResponse)
    def evaluation_report_html(run_id: str) -> str:
        if not _RUN_ID_RE.match(run_id):
            raise HTTPException(400, "invalid run id")
        return _render_report_page(run_id)

    @app.get("/api/history")
    def history(limit: int = 50) -> list[dict[str, Any]]:
        return [asdict(r) for r in
                store.list_runs(limit=max(1, min(limit, 200)))]

    @app.get("/api/artifacts/{run_id}/{name}")
    def artifact(run_id: str, name: str) -> Response:
        if not _RUN_ID_RE.match(run_id):
            raise HTTPException(400, "invalid run id")
        if name not in _ARTIFACT_ALLOWLIST:
            raise HTTPException(404, "artifact not available")
        if name == "report.html":
            return Response(content=_render_report_page(run_id),
                            media_type="text/html")
        path = base / "runs" / run_id / name
        if not path.is_file():
            raise HTTPException(404, "artifact not found")
        return Response(content=path.read_bytes(),
                        media_type=_ARTIFACT_ALLOWLIST[name])

    return app


__all__ = ["create_app"]
