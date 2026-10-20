# Known Limitations

Concrete, not generic. Each limitation names a specific scenario where a
verdict would be unreliable, and what to do about it.

## 1. No trusted oracle → no confirmed correctness verdict

**Unreliable-verdict scenario:** the LLM generates 12 candidate tests for a
submission, but every candidate's expected output lands `provisional` or
`inconclusive` (e.g. the assignment's worked examples are ambiguous and no
instructor reference was provided). The correctness analyzer then reports
`inconclusive`, the run is marked `incomplete`, and **no grade is issued** —
even if the student's program is actually correct.
**Mitigation:** provide a Tier-1 oracle: an instructor reference solution
(`oracle.reference_solution_path`) or expected outputs
(`oracle.expected_outputs_path`), or write the assignment's worked examples
unambiguously so the Tier-2 spec-example oracle can anchor. The report's
`limitations` always state which tier was missing.

## 2. Timing measurements never prove Big-O

**Unreliable-verdict scenario:** a student's O(n²) sort passes the
correctness tests and the timing leg shows "fast" on the small generated
inputs (n ≤ 1000, where n² is still cheap). A timing-only read would call
it O(n log n) — wrongly.
**Mitigation:** the complexity analyzer scores from **static inference**
(loop-nesting depth, recursion detection) against `rules.complexity.required`;
timing is recorded for corroboration only and every report carries the
limitation "timing measurements are recorded for corroboration only and are
never used as proof of a Big-O class". State the required class in the
rules file.

## 3. LLM non-determinism moves evidence between runs

**Unreliable-verdict scenario:** two runs of the same submission produce
slightly different candidate test sets; one run's borderline test reaches
`trusted` and the other stays `provisional`, shifting the correctness score
by ~3 points and the final grade from 78.4 to 75.9.
**Mitigation:** temperature 0 + fixed seeds per task; the trust gate caps
how far a flaky guess can move the score (it can only ever become
`provisional`, never a confirmed failure); scoring itself is deterministic
given evidence. Acceptance bar: max deviation ≤ 7.0 across repeated runs
(measured 0.00 with deterministic stand-in; real-LLM measurement belongs on
the Ollama machine — see [docs/VARIANCE.md](docs/VARIANCE.md)).

## 4. Similarity is resemblance, not authorship proof

**Unreliable-verdict scenario:** two students independently write the
obvious 10-line solution to "add two integers"; token-normalized shingle
similarity is 0.92, above the flag threshold. Treating the flag as a
cheating verdict would punish independent correct work.
**Mitigation:** the analyzer **flags for human review only** — the code,
report language, and docs never claim authorship or LLM-authorship
detection (asserted by a unit test). Instructors should use flags as triage,
compare flagged pairs by hand, and set the corpus/threshold per assignment
(`rules.similarity`).
