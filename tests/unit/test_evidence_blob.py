"""Unit tests: evidence blob integrity (D28).

A finalized evidence record must never cite a blob hash whose bytes were
not persisted. append_blob verifies the bytes read back from the store;
verify_blob detects later tampering or loss.
"""

from __future__ import annotations

import hashlib

import pytest

from autograder.app.orchestration.evidence import EvidenceError, EvidenceRegistry
from autograder.app.persistence import RunStore


@pytest.fixture()
def store(tmp_path):
    s = RunStore(tmp_path)
    yield s
    s.close()


@pytest.fixture()
def registry(store):
    rec = store.create_run(submission_hash="sha256:s",
                           assignment_hash="sha256:a",
                           rules_hash="sha256:r", rules_version="1.0.0")
    return EvidenceRegistry(rec.id, store), rec.id


def _blob_path(store, run_id, blob: bytes):
    digest = hashlib.sha256(blob).hexdigest()
    return store.base_dir / "runs" / run_id / "blobs" / f"{digest}.bin"


def test_append_blob_persists_and_verifies(registry):
    reg, run_id = registry
    ev_id = reg.append_blob("tool_output", "compiler", b"gcc: all good")
    ev = reg.get(ev_id)
    assert ev.data["blob_sha256"] == hashlib.sha256(b"gcc: all good").hexdigest()
    assert reg.verify_blob(ev_id) is True


def test_verify_blob_detects_tampering(registry):
    reg, run_id = registry
    blob = b"original evidence bytes"
    ev_id = reg.append_blob("tool_output", "sandbox", blob)
    path = _blob_path(reg._store, run_id, blob)
    path.write_bytes(b"tampered bytes")
    assert reg.verify_blob(ev_id) is False


def test_verify_blob_detects_missing_blob(registry):
    reg, run_id = registry
    blob = b"soon to vanish"
    ev_id = reg.append_blob("tool_output", "sandbox", blob)
    _blob_path(reg._store, run_id, blob).unlink()
    assert reg.verify_blob(ev_id) is False


def test_append_same_blob_twice_is_deduped(registry):
    reg, _ = registry
    first = reg.append_blob("tool_output", "sandbox", b"same bytes")
    second = reg.append_blob("tool_output", "sandbox", b"same bytes")
    assert first != second  # two records...
    assert reg.verify_blob(first) is True  # ...one shared, intact blob
    assert reg.verify_blob(second) is True


class _FailingStore:
    """Store whose writes always fail (disk full, permissions, ...)."""

    def write_artifact(self, run_id, name, data):
        raise OSError("disk full")

    def read_artifact(self, run_id, name):
        raise KeyError(name)


def test_append_blob_failure_propagates():
    reg = EvidenceRegistry("run1", _FailingStore())
    with pytest.raises(EvidenceError):
        reg.append_blob("tool_output", "sandbox", b"payload")
    assert len(reg) == 0  # no record citing unpersisted bytes


class _LyingStore:
    """Store that accepts writes but returns different bytes on read."""

    def __init__(self):
        self._data = {}

    def write_artifact(self, run_id, name, data):
        self._data[name] = b"different bytes entirely"

    def read_artifact(self, run_id, name):
        return self._data[name]


def test_append_blob_detects_store_mismatch():
    reg = EvidenceRegistry("run1", _LyingStore())
    with pytest.raises(EvidenceError, match="mismatch"):
        reg.append_blob("tool_output", "sandbox", b"payload")
    assert len(reg) == 0


def test_append_blob_without_store_is_record_only():
    reg = EvidenceRegistry("run1", None)
    ev_id = reg.append_blob("tool_output", "sandbox", b"payload")
    assert reg.get(ev_id).data["blob_sha256"]
    # Nothing persisted anywhere -> integrity cannot be confirmed.
    assert reg.verify_blob(ev_id) is False
