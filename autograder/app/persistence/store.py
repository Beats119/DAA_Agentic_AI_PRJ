"""SQLite-backed run metadata + immutable artifact storage.

Design:
- SQLite (stdlib) holds run metadata, rule versions, assignments, and an
  artifact index. Filesystem holds the artifacts themselves under
  ``artifacts/runs/<run_id>/``.
- Artifact writes are atomic (temp file + ``os.replace``) and refuse to
  overwrite an existing name — no silent clobbering, no cross-run writes
  (every path is scoped under the run's own directory; ``..`` rejected).
- Once a run reaches a terminal state (completed / completed_with_warnings /
  failed / incomplete) it is *immutable*: further artifact writes or state
  changes raise :class:`ImmutableRunError`. Evidence is append-only.
- Run state transitions go through an explicit state machine; invalid jumps
  raise :class:`InvalidTransitionError` instead of corrupting history.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path


class PersistenceError(Exception):
    """Base class for persistence failures."""


class RunNotFoundError(PersistenceError):
    """No run with the given id exists."""


class InvalidTransitionError(PersistenceError):
    """The requested run-state transition is not allowed."""


class ImmutableRunError(PersistenceError):
    """The run is finalized; its records may no longer change."""


TERMINAL_STATES = frozenset(
    {"completed", "completed_with_warnings", "failed", "incomplete"}
)

# Explicit state machine. Every edge must be deliberate; anything else raises.
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"validating", "failed"}),
    "validating": frozenset({"planning", "failed", "incomplete"}),
    "planning": frozenset({"generating_tests", "failed", "incomplete"}),
    "generating_tests": frozenset({"executing", "failed", "incomplete"}),
    "executing": frozenset({"analyzing", "failed", "incomplete"}),
    "analyzing": frozenset({"scoring", "failed", "incomplete"}),
    "scoring": frozenset({"reporting", "failed", "incomplete"}),
    "reporting": frozenset(
        {"completed", "completed_with_warnings", "failed", "incomplete"}
    ),
    "completed": frozenset(),
    "completed_with_warnings": frozenset(),
    "failed": frozenset(),
    "incomplete": frozenset(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    submission_hash TEXT NOT NULL,
    assignment_hash TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    grade TEXT
);
CREATE TABLE IF NOT EXISTS rule_versions (
    hash TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    spec_hash TEXT NOT NULL,
    source_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, name),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _synchronized(method):
    """Serialize DB access across request and evaluation threads (RLock)."""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


@dataclass(frozen=True)
class RunRecord:
    """Metadata snapshot of one evaluation run."""

    id: str
    state: str
    created_at: str
    updated_at: str
    submission_hash: str
    assignment_hash: str
    rules_hash: str
    rules_version: str
    grade: str | None


@dataclass(frozen=True)
class ArtifactRecord:
    run_id: str
    name: str
    path: str
    sha256: str
    created_at: str


class RunStore:
    """Run metadata + artifact storage rooted at ``base_dir``."""

    def __init__(self, base_dir: str | Path, db_name: str = "autograder.db") -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_root = self.base_dir / "runs"
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.base_dir / db_name
        # check_same_thread=False + a re-entrant lock: the store is shared
        # between request threads (FastAPI threadpool) and background
        # evaluation threads. All DB access is serialized by the lock.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- runs -----------------------------------------------------------
    @_synchronized
    def create_run(
        self,
        submission_hash: str,
        assignment_hash: str,
        rules_hash: str,
        rules_version: str,
        run_id: str | None = None,
    ) -> RunRecord:
        rid = run_id or uuid.uuid4().hex
        now = _utcnow_iso()
        try:
            self._conn.execute(
                "INSERT INTO runs (id, state, created_at, updated_at,"
                " submission_hash, assignment_hash, rules_hash, rules_version, grade)"
                " VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, NULL)",
                (rid, now, now, submission_hash, assignment_hash, rules_hash, rules_version),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise PersistenceError(f"run {rid} already exists") from exc
        return self.get_run(rid)

    @_synchronized
    def get_run(self, run_id: str) -> RunRecord:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(f"no run {run_id}")
        return RunRecord(**{k: row[k] for k in row.keys()})

    @_synchronized
    def list_runs(self, state: str | None = None, limit: int = 100) -> list[RunRecord]:
        if state is None:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE state = ? ORDER BY created_at DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        return [RunRecord(**{k: r[k] for k in r.keys()}) for r in rows]

    @_synchronized
    def transition_run(self, run_id: str, to_state: str) -> RunRecord:
        """Move a run to a new state; rejects invalid jumps and terminal edits."""
        record = self.get_run(run_id)
        if record.state in TERMINAL_STATES:
            raise ImmutableRunError(
                f"run {run_id} is finalized ({record.state}); no further transitions"
            )
        allowed = TRANSITIONS.get(record.state, frozenset())
        if to_state not in allowed:
            raise InvalidTransitionError(
                f"cannot transition run {run_id} from {record.state!r} to {to_state!r}; "
                f"allowed: {sorted(allowed) or 'none'}"
            )
        self._conn.execute(
            "UPDATE runs SET state = ?, updated_at = ? WHERE id = ?",
            (to_state, _utcnow_iso(), run_id),
        )
        self._conn.commit()
        return self.get_run(run_id)

    @_synchronized
    def set_grade(self, run_id: str, grade: str | None) -> RunRecord:
        record = self.get_run(run_id)
        if record.state in TERMINAL_STATES:
            raise ImmutableRunError(f"run {run_id} is finalized; grade is immutable")
        self._conn.execute(
            "UPDATE runs SET grade = ?, updated_at = ? WHERE id = ?",
            (grade, _utcnow_iso(), run_id),
        )
        self._conn.commit()
        return self.get_run(run_id)

    # -- rule versions / assignments ------------------------------------
    @_synchronized
    def record_rule_version(self, rules_hash: str, version: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO rule_versions (hash, version, created_at)"
            " VALUES (?, ?, ?)",
            (rules_hash, version, _utcnow_iso()),
        )
        self._conn.commit()

    @_synchronized
    def get_rule_version(self, rules_hash: str) -> str | None:
        row = self._conn.execute(
            "SELECT version FROM rule_versions WHERE hash = ?", (rules_hash,)
        ).fetchone()
        return row["version"] if row else None

    @_synchronized
    def record_assignment(self, assignment_id: str, spec_hash: str, source_path: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO assignments (id, spec_hash, source_path, created_at)"
            " VALUES (?, ?, ?, ?)",
            (assignment_id, spec_hash, source_path, _utcnow_iso()),
        )
        self._conn.commit()

    # -- artifacts ------------------------------------------------------
    def _run_dir(self, run_id: str) -> Path:
        # get_run raises RunNotFoundError for unknown ids — no orphan dirs.
        record = self.get_run(run_id)
        if record.state in TERMINAL_STATES:
            raise ImmutableRunError(
                f"run {run_id} is finalized ({record.state}); artifacts are immutable"
            )
        run_dir = self.artifact_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    @staticmethod
    def _safe_name(name: str) -> str:
        if not name or name.startswith("/") or ".." in Path(name).parts:
            raise PersistenceError(f"unsafe artifact name: {name!r}")
        return name

    @_synchronized
    def write_artifact(self, run_id: str, name: str, data: bytes) -> ArtifactRecord:
        """Atomically store an artifact; refuses overwrites and finalized runs."""
        import hashlib

        safe = self._safe_name(name)
        run_dir = self._run_dir(run_id)  # raises if finalized/unknown
        target = run_dir / safe
        if target.exists():
            raise PersistenceError(
                f"artifact {safe!r} already exists for run {run_id}; "
                "artifacts are append-only"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp_")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        now = _utcnow_iso()
        self._conn.execute(
            "INSERT INTO artifacts (run_id, name, path, sha256, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, safe, str(target), digest, now),
        )
        self._conn.commit()
        return ArtifactRecord(run_id, safe, str(target), digest, now)

    @_synchronized
    def read_artifact(self, run_id: str, name: str) -> bytes:
        row = self._conn.execute(
            "SELECT path FROM artifacts WHERE run_id = ? AND name = ?",
            (run_id, self._safe_name(name)),
        ).fetchone()
        if row is None:
            raise PersistenceError(f"no artifact {name!r} for run {run_id}")
        return Path(row["path"]).read_bytes()

    @_synchronized
    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        self.get_run(run_id)  # raises if unknown
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [ArtifactRecord(**{k: r[k] for k in r.keys()}) for r in rows]

    @_synchronized
    def close(self) -> None:
        self._conn.close()
