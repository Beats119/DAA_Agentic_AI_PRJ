"""Similarity analyzer: token-level resemblance against a corpus.

Method: strip comments/strings/whitespace, optionally normalize identifiers,
shingle the token stream (k from params), and compute Jaccard similarity
against each corpus file. Score = 1 - max_similarity. Files at or above the
flag threshold are reported for HUMAN REVIEW.

What this is NOT (stated plainly, and asserted by tests):
- It is not proof of authorship or plagiarism.
- It is not AI-authorship detection and must never be presented as such.
A high score means "resembles corpus file X this much" — nothing more.
The instructor decides what it means.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..base import AnalyzerContext, build_result

ANALYZER_ID = "similarity"

DEFAULTS = {
    "shingle_k": 5,
    "normalize_identifiers": True,
    "corpus_dir": None,  # fallback when rules.similarity.corpus_paths is empty
    "max_corpus_files": 200,
}

REVIEW_DISCLAIMER = (
    "similarity is a resemblance measure only — it is not proof of "
    "authorship, plagiarism, or AI-generated code"
)

_C_KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do",
    "double", "else", "enum", "extern", "float", "for", "goto", "if", "inline",
    "int", "long", "register", "restrict", "return", "short", "signed",
    "sizeof", "static", "struct", "switch", "typedef", "union", "unsigned",
    "void", "volatile", "while", "_Bool", "_Complex", "_Imaginary",
    "include", "define", "NULL",
}

_TOKEN_RE = re.compile(r"[A-Za-z_]\w*|\d+(?:\.\d+)?|[^\s\w]")


def _strip_noise(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    source = re.sub(r"//[^\n]*", " ", source)
    source = re.sub(r'"(?:[^"\\]|\\.)*"', '"STR"', source)
    source = re.sub(r"'(?:[^'\\]|\\.)*'", "'C'", source)
    return source


def _tokenize(source: str, normalize_identifiers: bool) -> list[str]:
    tokens = _TOKEN_RE.findall(_strip_noise(source))
    if not normalize_identifiers:
        return tokens
    out: list[str] = []
    for tok in tokens:
        if re.fullmatch(r"[A-Za-z_]\w*", tok) and tok not in _C_KEYWORDS:
            out.append("ID")
        else:
            out.append(tok)
    return out


def _shingles(tokens: list[str], k: int) -> set[tuple[str, ...]]:
    if len(tokens) < k:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def jaccard(a: set, b: set) -> float:
    """Jaccard similarity of two shingle sets. Pure function (unit-tested)."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def analyze(ctx: AnalyzerContext) -> Any:
    params = {**DEFAULTS, **ctx.metric.params}
    corpus_paths: list[str] = list(ctx.rules.similarity.corpus_paths or [])
    if params["corpus_dir"]:
        corpus_paths.append(str(params["corpus_dir"]))

    corpus_files: list[Path] = []
    for cp in corpus_paths:
        p = Path(cp)
        if p.is_dir():
            corpus_files.extend(sorted(p.glob("*.c")))
        elif p.is_file():
            corpus_files.append(p)
    # Never compare the submission against itself.
    src_resolved = str(Path(ctx.source_path).resolve())
    corpus_files = [f for f in corpus_files if str(f.resolve()) != src_resolved]
    corpus_files = corpus_files[: int(params["max_corpus_files"])]

    if not corpus_files:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="skipped",
            diagnostics={"reason": "no corpus files configured or found"},
            warnings=["similarity corpus is empty — nothing to compare against"],
        )

    try:
        source = Path(ctx.source_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="failed",
            diagnostics={"reason": f"could not read source: {exc}"},
        )

    k = int(params["shingle_k"])
    norm = bool(params["normalize_identifiers"])
    src_shingles = _shingles(_tokenize(source, norm), k)

    comparisons: list[dict[str, Any]] = []
    for cf in corpus_files:
        try:
            text = cf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sim = jaccard(src_shingles, _shingles(_tokenize(text, norm), k))
        comparisons.append({"file": cf.name, "similarity": round(sim, 4)})
    comparisons.sort(key=lambda c: c["similarity"], reverse=True)

    if not comparisons:
        return build_result(
            metric=ctx.metric,
            analyzer_id=ANALYZER_ID,
            status="inconclusive",
            diagnostics={"reason": "corpus files could not be read"},
        )

    max_sim = comparisons[0]["similarity"]
    score = max(0.0, 1.0 - max_sim)
    threshold = float(ctx.rules.similarity.threshold)
    flagged = [c for c in comparisons if c["similarity"] >= threshold]

    findings: list[str] = []
    for c in flagged:
        findings.append(
            f"{c['file']}: Jaccard similarity {c['similarity']:.2f} >= flag "
            f"threshold {threshold:.2f} — flagged for HUMAN REVIEW. "
            f"{REVIEW_DISCLAIMER}."
        )
    if not flagged:
        findings.append(
            f"highest corpus similarity {max_sim:.2f} "
            f"({comparisons[0]['file']}) — below flag threshold {threshold:.2f}"
        )

    evidence_refs = [
        ctx.add_evidence(
            ANALYZER_ID,
            "similarity",
            {
                "comparisons": comparisons,
                "max_similarity": max_sim,
                "flag_threshold": threshold,
                "flagged": [c["file"] for c in flagged],
                "shingle_k": k,
                "normalize_identifiers": norm,
                "disclaimer": REVIEW_DISCLAIMER,
            },
            confidence=0.85,
        )
    ]

    return build_result(
        metric=ctx.metric,
        analyzer_id=ANALYZER_ID,
        status="completed",
        score=score,
        measurements={
            "max_similarity": max_sim,
            "most_similar_file": comparisons[0]["file"],
            "flagged_count": len(flagged),
            "corpus_size": len(comparisons),
        },
        evidence_refs=evidence_refs,
        findings=findings,
        limitations=[REVIEW_DISCLAIMER],
    )
