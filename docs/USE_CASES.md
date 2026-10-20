# Real-World Use Cases

## 1. Weekly lab grading at scale

A DAA course with 120 students and weekly C assignments. The instructor
uploads the assignment PDF once, writes one rules file per assignment
(weights, limits, required complexity), and bulk-submits student programs.
Each submission gets a sandboxed evaluation: fresh LLM-generated tests,
tiered oracle validation, 8 analyzers, deterministic grade, and a student
report. The instructor reviews only `completed_with_warnings` /
`incomplete` runs and similarity flags — the common case needs no manual
test authoring.

## 2. Formative feedback for students

Beyond the grade, each report carries evidence-grounded feedback: which
trusted tests failed (with input, expected, and actual output), compiler
warnings with file/line, sanitizer findings, and complexity notes. Because
feedback is deterministically verified against recorded evidence, a student
can trust that every cited failure really happened on their program — and
instructors can share reports directly without vetting each claim.

## 3. Plagiarism triage across a cohort

With `rules.similarity.corpus_paths` pointed at the submission directory,
every run is compared token-wise against its peers. High-similarity pairs
are flagged **for human review** (never auto-verdicts). The instructor gets
a ranked triage list instead of eyeballing 120 files, and the report
language keeps the flag honest about what it is: resemblance, not proof.

## 4. Regression harness for reference solutions

Course staff maintain a reference solution per assignment. Running the
reference through the pipeline each semester (as `oracle.reference_solution_path`)
validates it against the current spec extraction: if the reference stops
reproducing the spec's worked examples, the oracle downgrades it and the
run says so — catching stale references before they grade students.
The same flow works as a CI-style check on the assignment + rules
themselves before release.
