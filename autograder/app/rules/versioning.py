"""Rule versioning and content hashing.

Every validated ruleset gets a SHA-256 fingerprint of its *canonical* form
(sorted keys, compact separators) — the exact configuration the run graded
against. The hash is what reports and the run manifest record, so a grade
can always be traced back to the precise policy that produced it.
"""

from __future__ import annotations

import hashlib
import json
import re

from ..core.schemas import RuleSet


def canonical_json(rules: RuleSet) -> str:
    """Deterministic JSON of the validated ruleset (what was actually used)."""
    data = rules.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def hash_rules(rules: RuleSet) -> str:
    """``sha256:``-prefixed hex digest of the canonical rules JSON."""
    return "sha256:" + hashlib.sha256(canonical_json(rules).encode("utf-8")).hexdigest()


_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(.*)$")


def bump_version(version: str) -> str:
    """Increment a ``X.Y.Z`` version's patch number.

    Non-semver versions get ``-r2``/``-r3``… revision suffixes instead.
    Used when an instructor edits rules and wants a new recorded version.
    """
    m = _SEMVER.match(version.strip())
    if m:
        major, minor, patch, rest = m.groups()
        return f"{major}.{minor}.{int(patch) + 1}{rest}"
    rm = re.match(r"^(.*?)-r(\d+)$", version.strip())
    if rm:
        return f"{rm.group(1)}-r{int(rm.group(2)) + 1}"
    return f"{version.strip()}-r2"
