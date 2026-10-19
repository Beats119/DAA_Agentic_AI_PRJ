"""Unit tests: persistence — runs, state machine, artifacts, immutability."""

from __future__ import annotations

import pytest

from autograder.app.persistence import (
    ImmutableRunError,
    InvalidTransitionError,
    PersistenceError,
    RunNotFoundError,
    RunStore,
)


@pytest.fixture()
def store(tmp_path):
    s = RunStore(tmp_path)
    yield s
    s.close()


def make_run(store, **kw):
    args = dict(submission_hash="sha256:s", assignment_hash="sha256:a",
                rules_hash="sha256:r", rules_version="1.0.0")
    args.update(kw)
    return store.create_run(**args)


def test_create_and_get_run(store):
    rec = make_run(store)
    assert rec.state == "queued"
    assert store.get_run(rec.id).id == rec.id


def test_unknown_run_raises(store):
    with pytest.raises(RunNotFoundError):
        store.get_run("nope")


def test_full_state_walk(store):
    rec = make_run(store)
    for state in ["validating", "planning", "generating_tests", "executing",
                  "analyzing", "scoring", "reporting", "completed"]:
        rec = store.transition_run(rec.id, state)
    assert rec.state == "completed"


@pytest.mark.parametrize("bad", ["completed", "scoring", "planning"])
def test_invalid_transition_rejected(store, bad):
    rec = make_run(store)  # queued
    with pytest.raises(InvalidTransitionError):
        store.transition_run(rec.id, bad)


def test_terminal_state_immutable(store):
    rec = make_run(store)
    store.transition_run(rec.id, "failed")
    with pytest.raises(ImmutableRunError):
        store.transition_run(rec.id, "queued")
    with pytest.raises(ImmutableRunError):
        store.set_grade(rec.id, "50.0")
    with pytest.raises(ImmutableRunError):
        store.write_artifact(rec.id, "x.txt", b"data")


def test_set_grade_before_finalize(store):
    rec = make_run(store)
    rec = store.set_grade(rec.id, "68.0")
    assert rec.grade == "68.0"


def test_write_and_read_artifact(store):
    rec = make_run(store)
    art = store.write_artifact(rec.id, "report.json", b'{"a":1}')
    assert art.sha256.startswith("sha256:")
    assert store.read_artifact(rec.id, "report.json") == b'{"a":1}'
    assert store.list_artifacts(rec.id)[0].name == "report.json"


def test_artifact_no_overwrite(store):
    rec = make_run(store)
    store.write_artifact(rec.id, "r.json", b"1")
    with pytest.raises(PersistenceError, match="already exists"):
        store.write_artifact(rec.id, "r.json", b"2")
    assert store.read_artifact(rec.id, "r.json") == b"1"


def test_artifact_path_traversal_rejected(store):
    rec = make_run(store)
    with pytest.raises(PersistenceError):
        store.write_artifact(rec.id, "../evil.txt", b"x")
    with pytest.raises(PersistenceError):
        store.write_artifact(rec.id, "/abs.txt", b"x")


def test_artifacts_scoped_per_run(store):
    a = make_run(store)
    b = make_run(store)
    store.write_artifact(a.id, "r.json", b"A")
    store.write_artifact(b.id, "r.json", b"B")  # same name, different run: fine
    assert store.read_artifact(a.id, "r.json") == b"A"
    assert store.read_artifact(b.id, "r.json") == b"B"


def test_rule_version_recorded(store):
    store.record_rule_version("sha256:xyz", "2.0.0")
    assert store.get_rule_version("sha256:xyz") == "2.0.0"
    assert store.get_rule_version("sha256:nope") is None


def test_assignment_recorded(store):
    store.record_assignment("asg1", "sha256:spec", "/tmp/a.pdf")
    # idempotent re-record
    store.record_assignment("asg1", "sha256:spec", "/tmp/a.pdf")


def test_list_runs(store):
    make_run(store)
    make_run(store)
    assert len(store.list_runs()) == 2
    assert store.list_runs(state="completed") == []
