# AutoGrader

A locally-running, agentic evaluation platform for student-submitted C
programs. Give it an assignment specification (PDF), an instructor-written
grading rules file, and a student's C source — it generates candidate tests
with a local LLM (Ollama), validates them through a tiered oracle, executes
everything in an isolated Linux sandbox, and produces a deterministic,
evidence-grounded grade with JSON + HTML reports.

**Architecture and build plan:** see [docs/PLAN.md](docs/PLAN.md) — it is the
reconciled, governing spec (architecture diagrams + clarifications + master
development prompt).

**Status:** Phase 2 (core foundations) in progress. Full setup, launch and
test instructions land with the later phases.
