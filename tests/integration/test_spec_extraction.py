"""Spec-extraction agent tests — mock LLM returns canned spec JSON
(CLEARLY test-only). Validates the agent's post-processing contract."""

from __future__ import annotations

import json

from autograder.app.core.schemas import AssignmentDoc, PageText
from autograder.app.ollama.client import OllamaClient
from autograder.app.specification.agent import extract_specification

CANNED_SPEC = {
    "requirements": [
        {
            "id": "R1",
            "text": "Read two integers from stdin.",
            "kind": "explicit",
            "source": {"page": 1, "excerpt": "read two integers from standard input"},
        },
        {
            "id": "R2",
            "text": "Print their sum followed by a newline.",
            "kind": "explicit",
            "source": {"page": 1, "excerpt": "print their sum followed by a newline"},
        },
        {
            "id": "R3",
            "text": "Handle negative inputs.",
            "kind": "explicit",
            "source": {"page": 2, "excerpt": ""},  # no excerpt -> must demote
        },
    ],
    "examples": [{"input": "2 3\n", "output": "5\n", "page": 1}],
    "ambiguities": [
        {"text": "Overflow behavior unspecified.", "impact": "limits edge testing"}
    ],
    "extraction_diagnostics": {"pages_read": 2, "notes": "canned"},
}


def _doc():
    return AssignmentDoc(
        filename="add.pdf",
        sha256="sha256:" + "a" * 64,
        text="Add two integers. Read two integers from standard input. Print their sum.",
        pages=[PageText(page_no=1, text="Add two integers...")],
    )


def test_extract_spec_validates_and_demotes(mock_ollama):
    url, server = mock_ollama
    server.script["generate"] = [("json", {"response": json.dumps(CANNED_SPEC)})]
    client = OllamaClient(url)
    try:
        spec, record = extract_specification(_doc(), client)
    finally:
        client.close()
    kinds = {r.id: r.kind for r in spec.requirements}
    assert kinds["R1"] == "explicit"
    # R3 claimed explicit but gave no excerpt -> demoted to inferred.
    assert kinds["R3"] == "inferred"
    assert any("demoted" in n for n in record["post_validation_notes"])
    assert spec.examples[0].output == "5\n"
    assert spec.ambiguities[0].text.startswith("Overflow")
    assert record["generation"]["task"] == "specification"


def test_all_ambiguous_warns(mock_ollama):
    url, server = mock_ollama
    canned = {
        "requirements": [
            {
                "id": "R1",
                "text": "Something unclear.",
                "kind": "ambiguous",
                "source": {"page": 1, "excerpt": "vague text"},
            }
        ],
        "examples": [],
        "ambiguities": [],
        "extraction_diagnostics": {},
    }
    server.script["generate"] = [("json", {"response": json.dumps(canned)})]
    client = OllamaClient(url)
    try:
        spec, record = extract_specification(_doc(), client)
    finally:
        client.close()
    assert any("every requirement ambiguous" in n for n in record["post_validation_notes"])
    assert spec.extraction_diagnostics["counts"]["ambiguous"] == 1
