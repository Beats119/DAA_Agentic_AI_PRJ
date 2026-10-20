"""UI honesty tests (Phase J) + JSON/HTML report identity (Phase I).

The pages must never state what the run record does not support:

- a run with no grade shows no grade — on the dashboard, in history,
  and on its detail page — while a CONFIRMED 0.0 grade still displays
  as ``0.0`` (``None`` and ``0.0`` are different facts);
- a failed run's detail page says it failed and shows the recorded
  reason — it must not pose as still running or keep polling;
- a queued/running run shows its real state and polls;
- a fresh install renders helpful empty states, not errors;
- the operations page renders the live capability probe (measured
  values as values) and the mandatory isolation policy.

Runs are constructed directly in the store, the same records the
workflow writes. The JSON<->HTML identity test validates a report dict
through the real ``validate_report`` and renders it through the real
``render_report_html``, then checks every displayed number against the
JSON values.
"""

from __future__ import annotations

import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from autograder.app.api.app import create_app
from autograder.app.persistence.store import RunStore
from autograder.app.reporting.html import render_report_html
from autograder.app.reporting.schemas import validate_report
from autograder.app.scoring import POLICY_VERSION

_HASH = "sha256:" + "a" * 64


@pytest.fixture()
def client(tmp_path):
    base = tmp_path / "srv"
    app = create_app(base_dir=base, gateway_factory=lambda: None)
    with TestClient(app) as c:
        yield c, base


def _walk_to_reporting(store: RunStore, rid: str) -> None:
    for state in ("validating", "planning", "generating_tests",
                  "executing", "analyzing", "scoring", "reporting"):
        store.transition_run(rid, state)


def _make_run(base, *, grade: str | None = None,
              final: str | None = None) -> str:
    """Create a run record in the app's store; walk it to ``final``.

    ``grade`` is the stored TEXT grade, exactly as the workflow writes
    it (``str(grade_value)``) — set while the run is in ``reporting``.
    """
    store = RunStore(base)
    rid = uuid.uuid4().hex
    store.create_run(_HASH, _HASH, _HASH, "1.0.0", run_id=rid)
    if final is None:
        return rid
    if final == "failed":
        store.transition_run(rid, "failed")
        return rid
    _walk_to_reporting(store, rid)
    if grade is not None:
        store.set_grade(rid, grade)
    store.transition_run(rid, final)
    return rid


def _make_run_with_report(base, *, grade: str | None,
                          final: str) -> str:
    """A run whose report.json artifact exists, as the workflow leaves it:
    artifacts are written BEFORE the terminal transition (they are
    immutable afterwards)."""
    store = RunStore(base)
    rid = uuid.uuid4().hex
    store.create_run(_HASH, _HASH, _HASH, "1.0.0", run_id=rid)
    _walk_to_reporting(store, rid)
    status = "completed" if final == "completed" else "incomplete"
    store.write_artifact(
        rid, "report.json",
        json.dumps(_report_dict(rid, grade=grade, status=status)).encode())
    if grade is not None:
        store.set_grade(rid, str(grade))
    store.transition_run(rid, final)
    return rid


def _report_dict(run_id: str, *, grade, status: str) -> dict:
    """A validate_report-clean report dict (metrics reconcile by hand).

    correctness: configured 0.50, effective 1.00 (renormalized), score
    0.8 -> contribution 80.0. compiler: configured 0.50, effective 0.0,
    no score -> contribution 0.0. Grade 80.0 when present.
    """
    return {
        "run_id": run_id,
        "submission": {"id": "add", "source_hash": _HASH},
        "assignment": {"id": "a1", "spec_hash": _HASH},
        "rules": {"version": "1.0.0", "hash": _HASH},
        "grade": grade,
        "status": status,
        "metrics": [
            {"name": "correctness", "analyzer": "correctness",
             "weight": 1.0, "configured_weight": 0.5,
             "effective_weight": 1.0, "score": 0.8,
             "contribution": 80.0 if grade is not None else 0.0,
             "status": "completed"},
            {"name": "compiler", "analyzer": "compiler",
             "weight": 0.0, "configured_weight": 0.5,
             "effective_weight": 0.0, "score": None,
             "contribution": 0.0, "status": "inconclusive"},
        ],
        "analyzer_statuses": {"correctness": "completed",
                              "compiler": "inconclusive"},
        "scoring_policy_version": POLICY_VERSION,
        "reproduction": {"run_id": run_id, "rules_hash": _HASH,
                         "scoring_policy_version": POLICY_VERSION},
    }


# --------------------------------------------------------------------- #
# Empty states
# --------------------------------------------------------------------- #

def test_empty_dashboard_renders_empty_state(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "No evaluations yet" in r.text


def test_empty_history_is_an_empty_list(client):
    c, _ = client
    r = c.get("/api/history")
    assert r.status_code == 200
    assert r.json() == []


# --------------------------------------------------------------------- #
# Grades: None is not 0.0
# --------------------------------------------------------------------- #

def test_dashboard_shows_confirmed_zero_and_dash_for_none(client):
    c, base = client
    _make_run(base, grade="0.0", final="completed")
    _make_run(base, final="incomplete")
    r = c.get("/")
    assert r.status_code == 200
    assert '<td class="num">0.0</td>' in r.text  # confirmed zero shows
    assert '<td class="num">—</td>' in r.text    # no grade -> dash


def test_history_json_keeps_grade_null_not_zero(client):
    c, base = client
    rid_none = _make_run(base, final="incomplete")
    rid_zero = _make_run(base, grade="0.0", final="completed")
    rows = {row["id"]: row for row in c.get("/api/history").json()}
    assert rows[rid_none]["grade"] is None
    assert rows[rid_zero]["grade"] == "0.0"


def test_incomplete_run_detail_shows_status_and_no_grade(client):
    c, base = client
    rid = _make_run_with_report(base, grade=None, final="incomplete")
    r = c.get(f"/runs/{rid}")
    assert r.status_code == 200
    assert "incomplete" in r.text
    assert "No grade computed for this run" in r.text
    # The grade hero must be the dash, never a number.
    assert '<div class="grade-value">—</div>' in r.text
    assert not re.search(r'class="grade-value">\d', r.text)


def test_completed_run_detail_shows_its_grade(client):
    c, base = client
    rid = _make_run_with_report(base, grade=80.0, final="completed")
    r = c.get(f"/runs/{rid}")
    assert r.status_code == 200
    assert '<div class="grade-value">80.0</div>' in r.text


# --------------------------------------------------------------------- #
# Progress / failure honesty
# --------------------------------------------------------------------- #

def test_failed_run_detail_shows_reason_not_running(client):
    c, base = client
    # Startup recovery writes interrupted.txt BEFORE failing the run
    # (artifacts are immutable once terminal) — mirror that order.
    store = RunStore(base)
    rid = uuid.uuid4().hex
    store.create_run(_HASH, _HASH, _HASH, "1.0.0", run_id=rid)
    store.write_artifact(rid, "interrupted.txt",
                         b"run interrupted: interrupted by restart\n")
    store.transition_run(rid, "failed")
    r = c.get(f"/runs/{rid}")
    assert r.status_code == 200
    assert "failed" in r.text
    assert "interrupted by restart" in r.text  # the recorded reason
    assert "evaluation is running" not in r.text
    assert "AG.pollRun" not in r.text  # terminal: no polling script


def test_queued_run_detail_shows_state_and_polls(client):
    c, base = client
    rid = _make_run(base)  # stays "queued"
    r = c.get(f"/runs/{rid}")
    assert r.status_code == 200
    assert "queued" in r.text
    assert "evaluation is running" in r.text
    assert "AG.pollRun" in r.text


# --------------------------------------------------------------------- #
# Operations page: probe truth, not hardcoded claims
# --------------------------------------------------------------------- #

def test_operations_page_renders_probe_and_policy(client):
    c, _ = client
    r = c.get("/operations")
    assert r.status_code == 200
    # Live probe fields, rendered from probe_capabilities().
    for field in ("network_namespace", "seccomp", "landlock_abi",
                  "resource_limits"):
        assert field in r.text
    # The mandatory policy is listed; network isolation is mandatory.
    assert "Mandatory isolation policy" in r.text
    assert "network_namespace" in r.text
    # The stale Phase-4-era footnote must not survive.
    assert "process-group kill" not in r.text


# --------------------------------------------------------------------- #
# JSON <-> HTML identity
# --------------------------------------------------------------------- #

def test_report_html_matches_json_values():
    data = _report_dict("identity1", grade=80.0, status="completed")
    report = validate_report(data)
    html = render_report_html(report)

    # Grade and status.
    assert f'<div class="grade-value">{data["grade"]:.1f}</div>' in html
    assert f'badge-{data["status"]}' in html

    # Per-metric rows: configured weight, effective weight, score,
    # contribution — formatted exactly as the JSON values dictate.
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL)
    by_name = {}
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
        if cells and cells[0] in ("correctness", "compiler"):
            by_name[cells[0]] = [
                re.sub(r"<[^>]+>", "", cell).strip() for cell in cells]
    assert set(by_name) == {"correctness", "compiler"}
    for metric in data["metrics"]:
        cells = by_name[metric["name"]]
        assert cells[2] == f"{metric['configured_weight']:.2f}"
        assert cells[3] == f"{metric['effective_weight']:.2f}"
        expected_score = ("—" if metric["score"] is None
                          else f"{metric['score']:.3f}")
        assert cells[4] == expected_score
        assert cells[5] == f"{metric['contribution']:.2f}"

    # The renormalized weights differ in JSON, so they must differ in
    # HTML too (configured 0.50 shown next to effective 1.00).
    assert by_name["correctness"][2] == "0.50"
    assert by_name["correctness"][3] == "1.00"


def test_report_html_no_grade_matches_json_none():
    data = _report_dict("identity2", grade=None, status="incomplete")
    report = validate_report(data)
    html = render_report_html(report)
    assert '<div class="grade-value">—</div>' in html
    assert "No grade computed for this run" in html
    assert not re.search(r'class="grade-value">\d', html)


def test_new_evaluation_selects_are_not_browser_required(client):
    """Regression (user report, 2026-10-20): the assignment/submission
    selects are either/or with the upload fields — marking them
    ``required`` made the browser block submission with "Please select
    an item in the list" even when files were uploaded. The JS performs
    the either/or check with a readable message instead."""
    c, _base = client
    resp = c.get("/new")
    assert resp.status_code == 200
    html = resp.text
    for field in ("assignment_id", "submission_id", "rules_id"):
        m = re.search(rf'<select[^>]*name="{field}"[^>]*>', html)
        assert m, f"select {field} missing"
        assert "required" not in m.group(0), (
            f"select {field} must not be browser-required (either/or field)"
        )
