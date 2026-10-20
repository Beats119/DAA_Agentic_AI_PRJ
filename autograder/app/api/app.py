"""FastAPI application: REST API + server-rendered UI.

All routes are thin, read-only views over the deterministic pipeline:
nothing here mutates grades or evidence. Uploads are size-capped,
stored under server-generated ids, and treated as untrusted content.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
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
from pydantic import BaseModel, ValidationError

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE.parent / "templates"
_STATIC_DIR = _HERE.parent / "static"

_MAX_PDF_BYTES = 20 * 1024 * 1024
_MAX_C_BYTES = 1 * 1024 * 1024
_MAX_RULES_BYTES = 512 * 1024

_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ARTIFACT_ALLOWLIST = {
    "report.json": "application/json",
    "report.html": "text/html",
    "evidence.json": "application/json",
    "manifest.json": "application/json",
    "source.c": "text/plain",
}


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


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


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

    def _save_meta() -> None:
        meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")

    store = RunStore(base)
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ag_eval")
    futures: dict[str, Future] = {}

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        yield
        pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="AutoGrader", version="0.1.0", lifespan=_lifespan)
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
            return {"error": str(exc)}

    def _versions() -> dict[str, str]:
        import sys

        return {
            "scoring_policy": POLICY_VERSION,
            "sandbox": "sandbox-1.0",
            "python": sys.version.split()[0],
            "app": "0.1.0",
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
                            "error": f"evaluation failed before start: {exc}"}
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
                            "error": f"evaluation thread crashed: {exc}"}
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
            sandbox_ok=bool(caps.get("resource_limits")),
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
        report_html = None
        if state["status"] in ("completed", "completed_with_warnings",
                               "incomplete", "failed"):
            try:
                from ..core.schemas import Report
                from ..reporting.html import render_report_html

                report = validate_report(_load_report(run_id))
                src_path = base / "runs" / run_id / "source.c"
                source = (src_path.read_text(encoding="utf-8", errors="replace")
                          if src_path.exists() else None)
                report_html = render_report_html(report, source)
            except HTTPException:
                report_html = None
        return tpl.get_template("run_detail.html").render(
            active="dash", run_id=run_id, status=state["status"],
            report_html=report_html)

    @app.get("/rules", response_class=HTMLResponse)
    def rules_page() -> str:
        return tpl.get_template("rules.html").render(
            active="rules", rules=_read_rules(rules_dir))

    @app.get("/operations", response_class=HTMLResponse)
    def operations() -> str:
        ollama = _ollama_status()
        return tpl.get_template("operations.html").render(
            active="ops", ollama=ollama, ollama_url=ollama.get("url", ""),
            sandbox=_sandbox_caps(), versions=_versions(),
            prompts=_prompt_versions())

    # ---------------- REST API ----------------
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "ollama": _ollama_status(),
                "sandbox": _sandbox_caps(), "versions": _versions()}

    def _store_upload(directory: Path, data: bytes, ext: str,
                      kind: str) -> dict[str, str]:
        uid = uuid.uuid4().hex
        (directory / f"{uid}{ext}").write_bytes(data)
        entry = {"id": uid, "sha256": _sha256(data), "size": len(data)}
        meta[kind][uid] = entry
        _save_meta()
        return entry

    @app.post("/api/assignments", status_code=201)
    async def upload_assignment(file: UploadFile = File(...)) -> dict[str, Any]:
        data = await file.read()
        if len(data) > _MAX_PDF_BYTES:
            raise HTTPException(413, "assignment too large (max 20 MB)")
        if not data.startswith(b"%PDF") and not data.lstrip().startswith(b"{"):
            # Accept PDFs and plain-text assignments; reject everything else.
            # Text must contain real content (not just control bytes).
            text_ok = False
            try:
                text = data.decode("utf-8")
                alnum = sum(1 for ch in text if ch.isalnum())
                text_ok = alnum >= 3
            except UnicodeDecodeError:
                text_ok = False
            if not text_ok:
                raise HTTPException(400, "not a PDF or text assignment")
        ext = ".pdf" if data.startswith(b"%PDF") else ".txt"
        entry = _store_upload(assignments_dir, data, ext, "assignments")
        pages = None
        if ext == ".pdf":
            try:
                from ..ingestion.pdf import ingest_assignment

                tmp = assignments_dir / f"{entry['id']}{ext}"
                pages = len(ingest_assignment(str(tmp)).pages)
            except Exception:
                pages = None
        return {**entry, "pages": pages}

    @app.post("/api/submissions", status_code=201)
    async def upload_submission(file: UploadFile = File(...)) -> dict[str, Any]:
        data = await file.read()
        if len(data) > _MAX_C_BYTES:
            raise HTTPException(413, "submission too large (max 1 MB)")
        if not data.strip():
            raise HTTPException(400, "empty submission")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "submission is not UTF-8 text")
        return _store_upload(submissions_dir, data, ".c", "submissions")

    @app.get("/api/rules")
    def list_rules() -> list[dict[str, Any]]:
        return [{k: v for k, v in r.items() if k != "path"}
                for r in _read_rules(rules_dir)]

    @app.post("/api/rules", status_code=201)
    async def upload_rules(file: UploadFile = File(...)) -> dict[str, Any]:
        data = await file.read()
        if len(data) > _MAX_RULES_BYTES:
            raise HTTPException(413, "rules file too large (max 512 KB)")
        uid = uuid.uuid4().hex
        # Preserve a real extension: the rules loader dispatches on it.
        raw_name = (file.filename or "").lower()
        ext = ".json"
        if raw_name.endswith((".yaml", ".yml")):
            ext = ".yaml"
        tmp = rules_dir / f".tmp_{uid}{ext}"
        try:
            tmp.write_bytes(data)
            try:
                rules = load_rules(tmp)
            except Exception as exc:
                raise HTTPException(422, f"invalid rules: {exc}")
            final = rules_dir / f"{uid}.json"
            tmp.rename(final)
            rh = hash_rules(rules)
            return {"id": uid, "version": rules.version,
                    "hash": rh if rh.startswith("sha256:") else "sha256:" + rh,
                    "metrics": [m.name for m in rules.metrics]}
        finally:
            if tmp.exists():
                tmp.unlink()

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
            raise HTTPException(422, f"invalid rules: {exc}")

        run_id = uuid.uuid4().hex
        assignment_path = next(assignments_dir.glob(f"{req.assignment_id}.*"))
        submission_path = submissions_dir / f"{req.submission_id}.c"

        def _run() -> None:
            gateway = None
            if gateway_factory is not None:
                try:
                    gateway = gateway_factory()
                except Exception:
                    gateway = None
            from ..orchestration.workflow import evaluate

            evaluate(str(assignment_path), str(submission_path),
                     rules["path"], gateway=gateway, run_id=run_id,
                     base_dir=base)

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
