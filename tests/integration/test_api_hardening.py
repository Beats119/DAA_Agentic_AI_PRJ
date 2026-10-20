"""API hardening tests (Phase F): streaming uploads, concurrency bound,
YAML rules round-trip, error hygiene, restart recovery.

Covers audit findings D17 (whole-upload buffering), D18 (YAML rules
silently unusable), D19 (error/path hygiene), D26 (interrupted runs).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import fitz  # PyMuPDF
import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from autograder.app.api.app import _strip_paths, create_app

TERMINAL = {"completed", "completed_with_warnings", "failed", "incomplete"}

ADD_C = """\
#include <stdio.h>
int main(void) {
    int a, b;
    if (scanf("%d %d", &a, &b) != 2) return 1;
    printf("%d\\n", a + b);
    return 0;
}
"""


@pytest.fixture()
def client(tmp_path):
    base = tmp_path / "srv"
    app = create_app(base_dir=base, gateway_factory=lambda: None)
    with TestClient(app) as c:
        yield c, base


def _tiny_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Add two integers. Print the sum.")
    data = doc.tobytes()
    doc.close()
    return data


def _upload(client, url, filename, data, content_type="application/octet-stream"):
    r = client.post(url, files={"file": (filename, data, content_type)})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------- D17: bounded, incremental uploads ----------------

def test_oversized_assignment_rejected_and_cleaned(client):
    c, base = client
    big = b"%PDF" + b"x" * (20 * 1024 * 1024)  # header + over the 20 MB cap
    r = c.post("/api/assignments",
               files={"file": ("big.pdf", big, "application/pdf")})
    assert r.status_code == 413
    # No stored file, no leftover temp file.
    assert list((base / "uploads" / "assignments").iterdir()) == []


def test_oversized_submission_rejected_and_cleaned(client):
    c, base = client
    big = b"x" * (1 * 1024 * 1024 + 1)  # just over the 1 MB cap
    r = c.post("/api/submissions",
               files={"file": ("big.c", big, "text/plain")})
    assert r.status_code == 413
    assert list((base / "uploads" / "submissions").iterdir()) == []


def test_oversized_rules_rejected_and_cleaned(client):
    c, base = client
    big = b" " * (600 * 1024)  # over the 512 KB cap
    r = c.post("/api/rules",
               files={"file": ("big.json", big, "application/json")})
    assert r.status_code == 413
    leftovers = [p for p in (base / "rules").iterdir()
                 if p.name.startswith(".tmp_")]
    assert leftovers == []


def test_concurrent_uploads_bounded(tmp_path):
    """At most max_concurrent uploads stream at once; excess get 429."""
    base = tmp_path / "srv"
    app = create_app(base_dir=base, gateway_factory=lambda: None,
                     max_concurrent_uploads=2)
    boundary = "XBOUNDARY"
    head = (f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; '
            'filename="s.c"\r\n'
            "Content-Type: text/plain\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    async def slow_body():
        yield head
        await asyncio.sleep(1.0)  # hold the upload slot mid-stream
        yield ADD_C.encode()
        yield tail

    async def run_all():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://test") as ac:
            async def post():
                return await ac.post(
                    "/api/submissions", content=slow_body(),
                    headers={"content-type":
                             f"multipart/form-data; boundary={boundary}"})
            return await asyncio.gather(*[post() for _ in range(4)])

    responses = asyncio.run(run_all())
    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 201, 429, 429], codes
    rejected = [r for r in responses if r.status_code == 429][0]
    assert rejected.json()["error"]["code"] == "too_many_uploads"
    # The two admitted uploads were stored completely.
    stored = list((base / "uploads" / "submissions").glob("*.c"))
    assert len(stored) == 2


# ---------------- D18: YAML rules work end to end ----------------

def _yaml_rules_bytes() -> bytes:
    src = (Path(__file__).resolve().parent.parent.parent
           / "examples" / "rules" / "add.json")
    return yaml.safe_dump(json.loads(src.read_text())).encode()


def test_yaml_rules_upload_list_and_evaluate(client):
    c, base = client
    r = c.post("/api/rules", files={
        "file": ("add.yaml", _yaml_rules_bytes(), "application/yaml")})
    assert r.status_code == 201, r.text
    body = r.json()
    # Stored with its real extension so the loader can dispatch on it.
    assert (base / "rules" / f"{body['id']}.yaml").exists()

    listed = c.get("/api/rules").json()
    assert any(entry["id"] == body["id"] for entry in listed)

    a = _upload(c, "/api/assignments", "a.pdf", _tiny_pdf_bytes(),
                "application/pdf")
    s = _upload(c, "/api/submissions", "add.c", ADD_C.encode(), "text/plain")
    r = c.post("/api/evaluations", json={
        "assignment_id": a["id"], "submission_id": s["id"],
        "rules_id": body["id"]})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]

    deadline = time.time() + 150
    status = None
    while time.time() < deadline:
        status = c.get(f"/api/evaluations/{run_id}").json()["status"]
        if status in TERMINAL:
            break
        time.sleep(2)
    assert status in TERMINAL and status != "failed", status


# ---------------- D19: error hygiene ----------------

def test_invalid_rules_422_has_no_paths_or_traceback(client):
    c, base = client
    r = c.post("/api/rules",
               files={"file": ("bad.json", b'{"nope": true}',
                             "application/json")})
    assert r.status_code == 422
    assert "invalid rules" in r.json()["detail"]
    assert str(base) not in r.text
    assert "Traceback" not in r.text


def test_strip_paths_unit():
    msg = ("cannot read rules file /home/someone/artifacts/rules/x.json: "
           "[Errno 2] No such file or directory: '/var/tmp/y.json'")
    cleaned = _strip_paths(msg)
    assert "/home/someone" not in cleaned
    assert "/var/tmp" not in cleaned


def test_unhandled_error_is_structured(tmp_path):
    base = tmp_path / "srv"
    app = create_app(base_dir=base, gateway_factory=lambda: None)
    # raise_server_exceptions=False: observe the response a real client
    # gets instead of the test portal re-raising the handled exception.
    with TestClient(app, raise_server_exceptions=False) as c:
        run_id = "a" * 32
        run_dir = base / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "report.json").write_text("{ not json", encoding="utf-8")
        r = c.get(f"/api/evaluations/{run_id}/report")
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "internal_error"
    assert "Traceback" not in r.text
    assert "/home/" not in r.text


def test_deleted_assignment_file_is_404_not_500(client):
    c, base = client
    a = _upload(c, "/api/assignments", "a.pdf", _tiny_pdf_bytes(),
                "application/pdf")
    s = _upload(c, "/api/submissions", "add.c", ADD_C.encode(), "text/plain")
    rules = c.get("/api/rules").json()
    stored = next((base / "uploads" / "assignments").glob(f"{a['id']}.*"))
    stored.unlink()  # the file vanished after upload
    r = c.post("/api/evaluations", json={
        "assignment_id": a["id"], "submission_id": s["id"],
        "rules_id": rules[0]["id"]})
    assert r.status_code == 404
    assert "missing" in r.json()["detail"]


# ---------------- D26: restart recovery ----------------

def test_restart_recovery_fails_interrupted_runs(tmp_path):
    from autograder.app.persistence import RunStore

    base = tmp_path / "srv"
    store = RunStore(base)
    rec = store.create_run(submission_hash="sha256:s",
                           assignment_hash="sha256:a",
                           rules_hash="sha256:r", rules_version="1.0.0")
    store.transition_run(rec.id, "validating")
    store.transition_run(rec.id, "planning")  # dies here, mid-run
    store.close()

    app = create_app(base_dir=base, gateway_factory=lambda: None)
    with TestClient(app) as c:  # lifespan runs the recovery sweep
        state = c.get(f"/api/evaluations/{rec.id}")
        assert state.status_code == 200
        assert state.json()["status"] == "failed"
        hist = c.get("/api/history").json()
        assert any(h["id"] == rec.id and h["state"] == "failed"
                   for h in hist)
