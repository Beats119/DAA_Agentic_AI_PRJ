"""Evidence registry: typed, append-only, immutable after finalize.

Every piece of evidence produced during a run gets a typed reference
``ev:<run>:<seq>``. The registry is append-only while the run is live and
raises on any mutation after :meth:`finalize`. Large blobs are stored by
content hash through the persistence store and referenced by ID — the
registry itself only ever carries small JSON-able records.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from ..core.schemas import Evidence


class EvidenceError(RuntimeError):
    """Misuse of the evidence registry (e.g. append after finalize)."""


class EvidenceRegistry:
    """Append-only typed evidence store for one evaluation run."""

    def __init__(self, run_id: str, store: Any | None = None) -> None:
        self.run_id = run_id
        self._store = store
        self._items: list[Evidence] = []
        self._by_id: dict[str, Evidence] = {}
        self._seq = 0
        self._finalized = False

    # ------------------------------------------------------------------ #
    @property
    def finalized(self) -> bool:
        return self._finalized

    def __len__(self) -> int:
        return len(self._items)

    # ------------------------------------------------------------------ #
    def append(
        self,
        kind: str,
        analyzer: str,
        data: dict[str, Any],
        *,
        severity: str | None = None,
        confidence: float | None = None,
        affects_grade: bool = True,
    ) -> str:
        """Mint an immutable Evidence record; return its typed id."""
        if self._finalized:
            raise EvidenceError(
                "evidence registry is finalized; refusing append "
                f"(kind={kind!r}, analyzer={analyzer!r})"
            )
        self._seq += 1
        ev = Evidence(
            id=f"ev:{self.run_id}:{self._seq}",
            analyzer=analyzer,
            run_id=self.run_id,
            timestamp=datetime.now(timezone.utc),
            kind=kind,
            data=data,
            severity=severity,
            confidence=confidence,
            affects_grade=affects_grade,
        )
        self._items.append(ev)
        self._by_id[ev.id] = ev
        return ev.id

    def append_blob(
        self,
        kind: str,
        analyzer: str,
        blob: bytes,
        meta: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        """Store a large blob by content hash; the record references the hash.

        The blob is persisted BEFORE the record is minted, and the record's
        hash is the hash of the bytes read back from the store — a record
        can never cite bytes that were not actually persisted (D28). If
        persistence fails, :class:`EvidenceError` is raised and no record
        is created.
        """
        digest = hashlib.sha256(blob).hexdigest()
        if self._store is not None:
            name = f"blobs/{digest}.bin"
            try:
                self._store.write_artifact(self.run_id, name, blob)
            except Exception:
                # The append-only store refuses overwrites; identical
                # content under the same name is fine (content-addressed
                # dedupe) — anything else falls through to the read-back
                # check below, which adjudicates.
                pass
            try:
                persisted = self._store.read_artifact(self.run_id, name)
            except Exception as exc:
                raise EvidenceError(
                    f"blob {digest} could not be persisted: {exc}"
                ) from exc
            persisted_digest = hashlib.sha256(persisted).hexdigest()
            if persisted_digest != digest:
                raise EvidenceError(
                    f"persisted blob hash mismatch: record would cite "
                    f"{digest}, store holds {persisted_digest}"
                )
            digest = persisted_digest
        data = dict(meta or {})
        data.update({"blob_sha256": digest, "blob_bytes": len(blob)})
        return self.append(kind, analyzer, data, **kwargs)

    def verify_blob(self, evidence_id: str) -> bool:
        """True iff the blob cited by a record is intact in the store.

        Recomputes the hash of the persisted bytes and compares it with
        the hash recorded at append time. Missing records without blob
        data, a missing store, a missing blob, and corrupted bytes all
        verify as False.
        """
        ev = self.get(evidence_id)
        digest = ev.data.get("blob_sha256")
        if not digest or self._store is None:
            return False
        try:
            persisted = self._store.read_artifact(
                self.run_id, f"blobs/{digest}.bin"
            )
        except Exception:
            return False
        return hashlib.sha256(persisted).hexdigest() == digest

    # ------------------------------------------------------------------ #
    def get(self, evidence_id: str) -> Evidence:
        try:
            return self._by_id[evidence_id]
        except KeyError:
            raise KeyError(f"unknown evidence id {evidence_id!r}") from None

    def all(self) -> list[Evidence]:
        return list(self._items)

    def finalize(self) -> list[Evidence]:
        """Freeze the registry. Any later append raises EvidenceError."""
        self._finalized = True
        return list(self._items)

    def to_json(self) -> str:
        """JSON dump of all records (for prompts and artifacts)."""
        return json.dumps(
            [ev.model_dump(mode="json") for ev in self._items], indent=1
        )
