<!-- © 2026 Cartman ApS. All rights reserved. -->

# Dataset-Level Completeness Gates

**Status:** Source of truth
**Purpose:** Checks that can only be evaluated across the whole authored
dataset, not on any single record — required companion to the five
per-type rubrics in this directory (see `scoring-model.md` §1: none of
these are enforced by JSON-schema validation or
`ps_service.ingestion.adapters.internal_seed.persist`, verified directly
against that code).

These gates restore, in the c4b weighted-rubric context, the coverage that
`test-data/rubrics/policy-standard-control-strict-rubrics.md` §5 provided
before it was retired.

---

## 1. Gates

A gate operates on the **accepted** subset only — a record with
`overall_score >= pass_threshold` on its own per-type rubric. Rejected
records are excluded before any gate below is evaluated.

| Gate ID | Check                                                                                                                                                                                        |
| ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DG-001  | Every active `PracticeArea` has ≥1 accepted `Policy` reachable via `OWNS`.                                                                                                                   |
| DG-002  | Every active `RiskPath` has ≥1 accepted `Control` reachable via `VERIFIED_BY`.                                                                                                               |
| DG-003  | Every accepted `Policy` has ≥1 accepted `Standard` reachable via `SUPPORTED_BY`.                                                                                                             |
| DG-004  | Every accepted `Standard` has ≥1 accepted `Control` reachable via `IMPLEMENTED_BY`.                                                                                                          |
| DG-005  | Every accepted `Capability` is linked to ≥1 active `PracticeArea` (via `COVERS`) and ≥1 active `RiskPath` (via `MITIGATED_BY`).                                                              |
| DG-006  | Every accepted `Capability` has exactly one governing `Policy` via `GOVERNED_BY` — code enforces "at most one," this gate additionally requires "at least one," i.e. no orphaned Capability. |
| DG-007  | No record that failed its per-type rubric (`overall_score < pass_threshold`) appears in the `nodes` or `edges` of the exported/persisted document.                                           |

DG-001 through DG-004 and DG-006 close the exact enforcement gaps
identified in `scoring-model.md` §1: `persist.py` enforces the
child-has-a-parent direction for `GOVERNED_BY`/`SUPPORTED_BY`/
`IMPLEMENTED_BY`, but never the parent-has-a-child direction, and enforces
no cardinality at all for `OWNS`/`COVERS`/`MITIGATED_BY`/`VERIFIED_BY`.

## 2. Failure Handling

If any gate fails, the run status is `FAILED_DATASET_GATE`. Per-record
acceptance is not affected — a record can individually pass its rubric and
still be part of a dataset that fails a gate (e.g. an accepted Policy with
no accepted Standard beneath it fails DG-003 even though the Policy itself
scored ≥ 80).

## 3. Execution Order

1. Build candidate records.
2. Score each against its type's per-type rubric (`policy-rubric.md`,
   `standard-rubric.md`, `control-rubric.md`, `practice-area-rubric.md`,
   `risk-path-rubric.md`).
3. Drop every record with `overall_score < pass_threshold`.
4. Recompute graph links after dropping records (a dropped Standard can
   orphan its Controls, etc.).
5. Evaluate DG-001 through DG-007 against the accepted-only graph.
6. Export/persist only if every gate passes; otherwise `FAILED_DATASET_GATE`
   and no export.
