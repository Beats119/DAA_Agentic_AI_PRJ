"""API + frontend integration tests (FastAPI TestClient, no network).

Evaluations run with ``gateway_factory=lambda: None`` — the honest degraded
path (no LLM on this machine). Terminal states must be
``completed``/``completed_with_warnings``/``incomplete``/``failed`` with a
validated report; never a fabricated grade.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import fitz  # PyMuPDF
import pytest
from fastapi.testclient import TestClient

from autograder.app.api.app import create_app
from autograder.app.reporting.schemas import validate_report

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

XSS_C = """\
#include <stdio.h>
// <script>alert(1)</script>
int main(void) { printf("hi\\n"); return 0; }
"""


def _tiny_pdf(path: Path, text: str = "Add two integers. Print the sum.") -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


@pytest.fixture()
def client(tmp_path):
    base = tmp_path / "srv"
    app = create_app(base_dir=base, gateway_factory=lambda: None)
    with TestClient(app) as c:
        yield c, base


def _upload(client, url, filename, data, content_type="application/octet-stream"):
    r = client.post(url, files={"file": (filename, data, content_type)})
    assert r.status_code == 201, r.text
    return r.json()


def _seed_run(client, c_source: str = ADD_C):
    pdf_path = Path(tempfile.mkdtemp()) / "a.pdf"
    _tiny_pdf(pdf_path)
    a = _upload(client, "/api/assignments", "a.pdf",
                pdf_path.read_bytes(), "application/pdf")
    s = _upload(client, "/api/submissions", "add.c",
                c_source.encode(), "text/plain")
    rules = client.get("/api/rules").json()
    assert rules, "expected seeded example rules"
    return a, s, rules[0]


# ---------------- health / pages ----------------

def test_health_reports_ollama_honestly(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # No Ollama on this machine: must say so explicitly, never fake it.
    assert body["ollama"]["reachable"] is False
    assert body["ollama"]["error"]
    assert body["sandbox"]["resource_limits"] is True
    assert body["versions"]["scoring_policy"]


def test_html_pages_ok(client):
    c, _ = client
    for path, marker in [
        ("/", "Recent runs"),
        ("/new", "New evaluation"),
        ("/rules", "Grading rules"),
        ("/operations", "Sandbox capabilities"),
        ("/static/app.js", "pollRun"),
    ]:
        r = c.get(path)
        assert r.status_code == 200, path
        assert marker in r.text, f"{path} missing {marker!r}"


# ---------------- uploads ----------------

def test_upload_assignment_and_submission(client):
    c, _ = client
    pdf_path = Path(tempfile.mkdtemp()) / "a.pdf"
    _tiny_pdf(pdf_path)
    a = _upload(c, "/api/assignments", "assign.pdf",
                pdf_path.read_bytes(), "application/pdf")
    assert a["sha256"].startswith("sha256:")
    assert a["pages"] == 1
    s = _upload(c, "/api/submissions", "add.c", ADD_C.encode(), "text/plain")
    assert s["sha256"].startswith("sha256:")


def test_upload_rejects_garbage(client):
    c, _ = client
    r = c.post("/api/assignments",
               files={"file": ("x.bin", b"\x00\x01\x02\x03", "application/octet-stream")})
    assert r.status_code == 400
    r = c.post("/api/submissions",
               files={"file": ("e.c", b"", "text/plain")})
    assert r.status_code == 400
    big = b"x" * (1 * 1024 * 1024 + 1)
    r = c.post("/api/submissions", files={"file": ("big.c", big, "text/plain")})
    assert r.status_code == 413


def test_rules_reject_invalid_accept_valid(client):
    c, base = client
    r = c.post("/api/rules",
               files={"file": ("bad.json", b'{"nope": true}', "application/json")})
    assert r.status_code == 422
    assert "invalid rules" in r.json()["detail"]
    # nothing persisted for invalid rules
    assert not list((base / "rules").glob(".tmp_*"))

    valid = (Path(__file__).resolve().parent.parent.parent
             / "examples" / "rules" / "add.json").read_bytes()
    r = c.post("/api/rules", files={"file": ("add.json", valid, "application/json")})
    assert r.status_code == 201
    body = r.json()
    assert body["version"]
    assert (base / "rules" / f"{body['id']}.json").exists()


# ---------------- evaluation lifecycle ----------------

def _wait_terminal(c, run_id, timeout=150):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/evaluations/{run_id}")
        assert r.status_code == 200, r.text
        status = r.json()["status"]
        if status in TERMINAL:
            return r.json()
        assert status not in ("failed",), f"unexpected terminal: {r.json()}"
        time.sleep(2)
    raise AssertionError(f"run {run_id} not terminal after {timeout}s")


def test_full_evaluation_degraded_path(client):
    c, base = client
    a, s, rules = _seed_run(c)
    r = c.post("/api/evaluations", json={
        "assignment_id": a["id"], "submission_id": s["id"],
        "rules_id": rules["id"]})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]

    # polling works while running
    r = c.get(f"/api/evaluations/{run_id}")
    assert r.status_code == 200

    final = _wait_terminal(c, run_id)
    assert final["status"] in ("completed", "completed_with_warnings",
                               "incomplete"), final

    # JSON report validates and reconciles
    r = c.get(f"/api/evaluations/{run_id}/report")
    assert r.status_code == 200
    report = validate_report(r.json())
    assert report.run_id == run_id
    assert report.status == final["status"]
    # degraded path: LLM steps skipped, recorded honestly
    text = json.dumps(r.json())
    assert "no LLM gateway" in text or "LLM" in text

    # HTML report renders, shows grade, no server paths
    r = c.get(f"/api/evaluations/{run_id}/report.html")
    assert r.status_code == 200
    html = r.text
    assert "Evaluation report" in html
    if report.grade is not None:
        assert f"{report.grade:.1f}" in html
    assert "/home/" not in html and "/tmp/" not in html

    # run detail page embeds the report
    r = c.get(f"/runs/{run_id}")
    assert r.status_code == 200
    assert "Evaluation report" in r.text

    # history lists it
    hist = c.get("/api/history").json()
    assert any(h["id"] == run_id for h in hist)

    # artifacts
    r = c.get(f"/api/artifacts/{run_id}/report.json")
    assert r.status_code == 200
    r = c.get(f"/api/artifacts/{run_id}/source.c")
    assert r.status_code == 200
    assert "scanf" in r.text


def test_xss_source_is_escaped(client):
    c, _ = client
    a, s, rules = _seed_run(c, c_source=XSS_C)
    r = c.post("/api/evaluations", json={
        "assignment_id": a["id"], "submission_id": s["id"],
        "rules_id": rules["id"]})
    run_id = r.json()["run_id"]
    _wait_terminal(c, run_id)
    html = c.get(f"/api/evaluations/{run_id}/report.html").text
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_artifact_allowlist(client):
    c, base = client
    a, s, rules = _seed_run(c)
    r = c.post("/api/evaluations", json={
        "assignment_id": a["id"], "submission_id": s["id"],
        "rules_id": rules["id"]})
    run_id = r.json()["run_id"]
    _wait_terminal(c, run_id)

    assert c.get(f"/api/artifacts/{run_id}/manifest.json").status_code == 200
    # not allowlisted
    assert c.get(f"/api/artifacts/{run_id}/feedback.json").status_code == 404
    assert c.get(f"/api/artifacts/{run_id}/assignment.bin").status_code == 404
    # traversal attempts rejected
    assert c.get(f"/api/artifacts/{run_id}/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
    assert c.get(f"/api/artifacts/{'a' * 32}/report.json").status_code == 404
    assert c.get("/api/artifacts/not-a-run-id/report.json").status_code == 400
    assert c.get(f"/api/evaluations/{run_id}/report").status_code == 200


def test_create_evaluation_validates_ids(client):
    c, _ = client
    a, s, rules = _seed_run(c)
    bad = {"assignment_id": "0" * 32, "submission_id": s["id"],
           "rules_id": rules["id"]}
    assert c.post("/api/evaluations", json=bad).status_code == 404
    bad = {"assignment_id": a["id"], "submission_id": s["id"],
           "rules_id": "nope"}
    assert c.post("/api/evaluations", json=bad).status_code == 404
