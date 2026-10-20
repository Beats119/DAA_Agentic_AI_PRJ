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


# ---------------- D26: WAL, cross-instance use, recovery ----------------

def test_wal_mode_and_busy_timeout(tmp_path):
    import sqlite3

    s = RunStore(tmp_path)
    try:
        mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        busy = s._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert busy >= 1000
    finally:
        s.close()
    # WAL persists in the DB file for other connections too.
    conn = sqlite3.connect(str(tmp_path / "autograder.db"))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_two_stores_share_one_db(tmp_path):
    """D26: two RunStore instances on one DB (API + workflow) must not
    die with 'database is locked' under concurrent writes."""
    import threading

    a = RunStore(tmp_path)
    b = RunStore(tmp_path)
    errors: list[Exception] = []

    def work(store, tag):
        try:
            for i in range(15):
                rec = store.create_run(
                    submission_hash=f"sha256:s{tag}{i}",
                    assignment_hash="sha256:a", rules_hash="sha256:r",
                    rules_version="1.0.0")
                store.transition_run(rec.id, "validating")
                store.write_artifact(rec.id, "r.json", b"{}")
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(a, "a")),
               threading.Thread(target=work, args=(b, "b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        assert len(a.list_runs(limit=100)) == 30
        assert len(b.list_runs(limit=100)) == 30
    finally:
        a.close()
        b.close()


def test_verify_artifact_detects_tampering(store, tmp_path):
    rec = make_run(store)
    store.write_artifact(rec.id, "report.json", b'{"a":1}')
    assert store.verify_artifact(rec.id, "report.json") is True
    path = tmp_path / "runs" / rec.id / "report.json"
    path.write_bytes(b'{"a":2}')  # tampered bytes, same name
    assert store.verify_artifact(rec.id, "report.json") is False
    path.unlink()  # deleted blob
    assert store.verify_artifact(rec.id, "report.json") is False
    assert store.verify_artifact(rec.id, "never-written.json") is False
    assert store.verify_run_artifacts(rec.id) == {"report.json": False}


def test_recover_interrupted_runs(store):
    stuck = make_run(store)
    store.transition_run(stuck.id, "validating")
    store.transition_run(stuck.id, "planning")
    done = make_run(store)
    store.transition_run(done.id, "failed")  # terminal: untouched

    # A cutoff at the stuck run's creation time must NOT recover it
    # (it was still being updated then).
    assert store.recover_interrupted_runs(before=stuck.created_at) == []

    recovered = store.recover_interrupted_runs()
    assert recovered == [stuck.id]
    assert store.get_run(stuck.id).state == "failed"
    names = [a.name for a in store.list_artifacts(stuck.id)]
    assert "interrupted.txt" in names
    assert b"interrupted by restart" in store.read_artifact(
        stuck.id, "interrupted.txt")
    # Idempotent: nothing left to recover.
    assert store.recover_interrupted_runs() == []


def test_reconcile_orphan_files(store, tmp_path):
    rec = make_run(store)
    store.write_artifact(rec.id, "kept.json", b"1")
    run_dir = tmp_path / "runs" / rec.id
    (run_dir / "orphan.bin").write_bytes(b"unreferenced")
    (run_dir / "blobs").mkdir()
    (run_dir / "blobs" / "deadbeef.bin").write_bytes(b"unreferenced")
    # A directory for an unknown run is left untouched.
    stranger = tmp_path / "runs" / ("f" * 32)
    stranger.mkdir()
    (stranger / "x.bin").write_bytes(b"not ours")

    assert store.reconcile_orphan_files() == 2
    assert (run_dir / "kept.json").exists()
    assert not (run_dir / "orphan.bin").exists()
    assert not (run_dir / "blobs" / "deadbeef.bin").exists()
    assert (stranger / "x.bin").exists()
    assert store.reconcile_orphan_files() == 0
