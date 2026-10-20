# Rules Reference

Rules are the instructor's grading policy: the assignment PDF says **what**
the student must do; the rules file says **how** it is graded. Rules are
versioned JSON or YAML, validated before every evaluation, and hashed
(sha256 of canonical JSON) into the report for provenance.

Full annotated example: `examples/rules/add.json`. It demonstrates every
section and contains **no expected test answers** — those come from the
assignment spec and the tiered oracle at evaluation time.

## Schema

| Key | Type | Meaning |
|-----|------|---------|
| `problem_id` | string | Problem identifier (required). |
| `assignment_ref` | string | Path/pointer to the assignment document (required). |
| `version` | string (semver) | Rules version; bumped on change, recorded in reports. |
| `limits` | object | `time_limit_s`, `memory_limit_mb`, `process_limit` (`process_limit` maps to sandbox `nproc`), `output_limit_bytes`. All must be non-negative. |
| `compile` | object | `compiler` (`gcc`/`clang`), `flags` (list), `std` (e.g. `c11`). |
| `metrics` | list | One entry per metric: `name`, `analyzer` (must be a known analyzer id), `weight` (≥ 0), `required` (bool), `params` (analyzer-specific, see [docs/ANALYZERS.md](docs/ANALYZERS.md)), `thresholds`. |
| `comparison_policy` | object | `whitespace`: `exact` \| `normalize_trailing`; `case_sensitive`: bool. |
| `test_generation` | object | `max_rounds` (≤ 3), `max_candidates`, `token_budget`, `exec_budget`. |
| `oracle` | object | `tier_preference` (ordered list, default `["tier1_reference", "tier2_independent", "tier3_llm"]`), `reference_solution_path` (optional), `expected_outputs_path` (optional JSON `{input: output}`), `quarantine_after_rounds`. |
| `complexity` | object | `required`: e.g. `"O(1)"` — compared against static inference. |
| `similarity` | object | `corpus_paths` (list of dirs), `threshold` (flag for human review above this). |
| `uncertainty_policy` | object | `required_metric_missing`: `incomplete` \| `provisional`; `optional_metric_missing`: `renormalize` \| `zero` \| `exclude`. |
| `models` | object | Task → model-config key overrides (keys must exist in `configs/models.yaml`). |
| `report` | object | `formats`: subset of `["json", "html"]`; `include_source_excerpt`: bool. |
| `normalize_weights` | bool | Default `false`. When `false`, weights must sum to 1.0 within 1e-9 or the rules are **rejected**. |

## Validation rules (rejection, not silent fixing)

`autograder/app/rules/validate.py` rejects: missing required fields, unknown
analyzer ids, wrong types, negative limits, bad thresholds, negative
weights, weights not summing to 1.0 within 1e-9 (unless `normalize_weights`
is explicitly `true`). Keys named `_comment` are stripped as a documented
convention. Uploading invalid rules via `POST /api/rules` returns 422.

## Versioning & hashing

`autograder/app/rules/versioning.py`: semver bump helper; `hash_rules()`
returns `sha256:` of the canonical JSON of the validated config. The hash
and version are stored with every run — a grade is always traceable to the
exact policy that produced it.
