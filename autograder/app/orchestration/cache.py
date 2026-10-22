"""Per-assignment cache: specification + oracle-validated test sets.

Product policy (owner decision, 2026-10-22): test generation happens
ONCE per assignment. The specification extracted from an assignment and
the test set the tiered oracle validated for a given rules config are
expensive, slow LLM work whose inputs do not include the student — so
they are cached per assignment and reused for every later evaluation of
the same assignment. Everything student-specific (compile, sandboxed
execution, analyzers, scoring, feedback) still runs fresh on every run.

Cache identity
--------------
An assignment *fingerprint* keys the cache file. It is a sha256 over a
canonical JSON payload of:

- the assignment file's raw bytes (as their own sha256),
- the configured model names for the ``specification`` and
  ``test_generation`` tasks (``configs/models.yaml``),
- the version + body hash of the ``specification``, ``test_generation``
  and ``test_refinement`` prompt templates.

The fingerprint is a pure function of those inputs — no timestamps, no
randomness — so identical assignments map to the same cache entry in
any process. The specification entry depends only on the fingerprint;
test sets are keyed *inside* the entry by the exact rules-config hash,
so a rules change keeps the spec but regenerates the tests.

Storage: one JSON document per fingerprint at
``<artifacts base>/assignment_cache/<fingerprint>.json``, written
atomically (temp file + ``os.replace``). A file that fails to parse or
validate is a cache MISS with a logged warning — never a run failure.

Only validated work is cached: the workflow writes an entry only when
spec extraction succeeded and at least one generated test reached a
``trusted`` oracle verdict. A failed/empty generation writes nothing,
so a later run retries instead of inheriting a bad set. Reuse never
promotes an oracle status: a cached set carries exactly the statuses
and provenance the original assessment decided.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field

from ..core.schemas import Specification, StrictModel, TestCase
from ..ollama.prompts import load_prompt, prompt_hash

log = logging.getLogger("autograder.workflow")

CACHE_FORMAT_VERSION = 1

# Prompt templates whose version/hash participate in the fingerprint:
# the specification prompt produces the cached spec; the generation and
# refinement prompts produce the cached candidates.
_FINGERPRINT_PROMPTS = ("specification", "test_generation", "test_refinement")
# Tasks whose configured model names participate in the fingerprint.
_FINGERPRINT_TASKS = ("specification", "test_generation")


class CachedSpec(StrictModel):
    """A cached specification plus its provenance."""

    created_at: datetime
    source_run_id: str = Field(min_length=1)
    specification: Specification


class CachedTestSet(StrictModel):
    """One rules-keyed, oracle-assessed test set with its run counts."""

    created_at: datetime
    source_run_id: str = Field(min_length=1)
    rules_hash: str = Field(min_length=1)
    tests: list[TestCase] = Field(default_factory=list)
    # test_stats counts from the producing run (generated / validated /
    # rejected / trusted) so a reusing run's report stays faithful to
    # what the original generation produced.
    counts: dict[str, int] = Field(default_factory=dict)


class AssignmentCacheEntry(StrictModel):
    """The whole cache document for one assignment fingerprint."""

    format_version: int = CACHE_FORMAT_VERSION
    fingerprint: str = Field(min_length=1)
    created_at: datetime
    model_names: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    spec: CachedSpec
    testsets: dict[str, CachedTestSet] = Field(default_factory=dict)


class FingerprintInfo(StrictModel):
    """The computed cache identity for one assignment (no file I/O)."""

    fingerprint: str
    model_names: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    prompt_hashes: dict[str, str] = Field(default_factory=dict)


def build_fingerprint(assignment_bytes: bytes) -> FingerprintInfo | None:
    """Compute the assignment fingerprint, or None if inputs are unreadable.

    Pure function of the assignment bytes, the configured model names
    and the prompt templates — deterministic across processes. Returns
    None (cache disabled for this run) when the model config or a
    prompt template cannot be read; the run then proceeds uncached
    rather than failing.
    """
    try:
        from ..ollama.models import model_cfg_for

        model_names = {
            task: str(model_cfg_for(task)["model"])
            for task in _FINGERPRINT_TASKS
        }
        prompt_versions: dict[str, str] = {}
        prompt_hashes: dict[str, str] = {}
        for name in _FINGERPRINT_PROMPTS:
            fm, body = load_prompt(name)
            prompt_versions[name] = str(fm.get("version", ""))
            prompt_hashes[name] = prompt_hash(body)
    except Exception as exc:  # noqa: BLE001 - cache is best-effort
        log.warning("assignment cache disabled: cannot build fingerprint "
                    "(%s)", exc)
        return None
    payload = {
        "assignment_sha256": "sha256:" + hashlib.sha256(
            assignment_bytes).hexdigest(),
        "models": model_names,
        "prompt_versions": prompt_versions,
        "prompt_hashes": prompt_hashes,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return FingerprintInfo(
        fingerprint=fingerprint,
        model_names=model_names,
        prompt_versions=prompt_versions,
        prompt_hashes=prompt_hashes,
    )


def cache_path(cache_dir: str | Path, fingerprint: str) -> Path:
    return Path(cache_dir) / f"{fingerprint}.json"


def load_cache_entry(
    cache_dir: str | Path, fingerprint: str
) -> AssignmentCacheEntry | None:
    """Load and validate a cache entry; None on miss or corruption.

    A corrupt or schema-invalid file is a MISS plus a logged warning —
    it must never crash a run.
    """
    path = cache_path(cache_dir, fingerprint)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("assignment cache unreadable at %s (%s); treating as "
                    "a miss", path, exc)
        return None
    try:
        entry = AssignmentCacheEntry.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001 - any invalid content is a miss
        log.warning("assignment cache at %s is corrupt/invalid (%s); "
                    "treating as a miss", path, exc)
        return None
    if entry.format_version != CACHE_FORMAT_VERSION:
        log.warning("assignment cache at %s has format_version %s "
                    "(expected %s); treating as a miss",
                    path, entry.format_version, CACHE_FORMAT_VERSION)
        return None
    if entry.fingerprint != fingerprint:
        log.warning("assignment cache at %s names fingerprint %s, not %s; "
                    "treating as a miss",
                    path, entry.fingerprint[:16], fingerprint[:16])
        return None
    return entry


def store_cache_entry(cache_dir: str | Path, entry: AssignmentCacheEntry) -> Path:
    """Persist an entry atomically (temp file in the same dir + rename)."""
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = cache_path(directory, entry.fingerprint)
    fd, tmp_name = tempfile.mkstemp(
        dir=directory, prefix=entry.fingerprint[:16] + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(entry.model_dump_json(indent=1).encode("utf-8"))
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def record_cache_write(
    cache_dir: str | Path,
    info: FingerprintInfo,
    *,
    run_id: str,
    spec: Specification,
    spec_is_fresh: bool,
    testset: CachedTestSet | None,
) -> AssignmentCacheEntry:
    """Merge a fresh spec and/or test set into the entry and persist it.

    ``spec_is_fresh`` distinguishes "this run extracted the spec" (the
    cached spec is replaced and its provenance updated) from "this run
    reused the cached spec and only produced a new test set" (the
    original spec provenance is preserved). Test sets for other rules
    hashes already in the entry are never clobbered.
    """
    now = datetime.now(timezone.utc)
    entry = load_cache_entry(cache_dir, info.fingerprint)
    if entry is None:
        entry = AssignmentCacheEntry(
            fingerprint=info.fingerprint,
            created_at=now,
            model_names=dict(info.model_names),
            prompt_versions=dict(info.prompt_versions),
            prompt_hashes=dict(info.prompt_hashes),
            spec=CachedSpec(
                created_at=now, source_run_id=run_id, specification=spec),
        )
    elif spec_is_fresh:
        entry.spec = CachedSpec(
            created_at=now, source_run_id=run_id, specification=spec)
    if testset is not None:
        entry.testsets[testset.rules_hash] = testset
    store_cache_entry(cache_dir, entry)
    return entry
