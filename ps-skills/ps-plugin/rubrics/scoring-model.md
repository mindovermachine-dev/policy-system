<!-- © 2026 Cartman ApS. All rights reserved. -->

# Authoring Rubric — Scoring Model

**Status:** Source of truth
**Purpose:** Define the one scoring mechanism shared by every per-type authoring
rubric in this directory (`policy-rubric.md`, `standard-rubric.md`,
`control-rubric.md`, `practice-area-rubric.md`, `risk-path-rubric.md`).
**Style:** Weighted numeric scoring, matching the Cosmos4Biz platform
Rubric/Scorecard model (`c4b.domain.models.rubric`, ADR-043) — the model this
policy system's own future policy-editor skill will score against.

---

## 1. Scope: Content Quality, Not Structural Validity — and What Code Actually Enforces

This rubric evaluates the **quality** of an authored Policy, Standard,
Control, PracticeArea, or RiskPath record — clarity, verifiability,
coherence, honesty about lifecycle state. It deliberately does not
re-litigate anything the intake pipeline (JSON-schema validation +
`ps_service.ingestion.adapters.internal_seed.persist`) already checks
mechanically. But "already checked mechanically" is narrower than it
sounds — verified directly against `persist.py`, not assumed from the docs:

| Edge                                            | Cardinality actually enforced in code                                                              |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `GOVERNED_BY` (Capability→Policy)               | ≤1 outbound per Capability                                                                         |
| `SUPPORTED_BY` (Policy→Standard)                | exactly 1 inbound per Standard                                                                     |
| `IMPLEMENTED_BY` (Standard→Control)             | exactly 1 inbound per Control                                                                      |
| `OWNS`, `COVERS`, `MITIGATED_BY`, `VERIFIED_BY` | **none** — referential integrity only (endpoints exist, labels match); no cardinality check at all |

Even the three enforced edges only constrain the _child-has-exactly-one-
(or-at-most-one)-parent_ direction. Nothing in code guarantees the reverse:
a Policy having ≥1 Standard, a Standard having ≥1 Control, or a Capability
having a governing Policy at all. Required properties and enum membership
per node type are enforced (JSON-schema validation, see AC-BI-001 in #95) —
a record failing that never reaches rubric scoring — but dataset-level
_completeness_ (does every PracticeArea actually own a Policy? does every
RiskPath actually have a verifying Control?) is enforced by **neither**
schema validation nor `persist.py`. See `dataset-gates.md` for those checks
— they are a required companion to this per-record rubric, not an optional
extra.

## 2. Authoring-Time Gate, Not a Maturity Gate

This rubric runs **at authoring time** — when a record is first generated or
hand-authored, before a policy manager has reviewed or approved it. Per
`ps-domain-concepts.md`, freshly authored records legitimately start in an
immature lifecycle state:

- `Policy.status = draft`
- `Standard.implementation_status = draft`
- `Control.implementation_status = planned`, with `execution_frequency`,
  `last_test_date`, `next_review_date`, and `evidence_ref` all null

A rubric criterion must never penalize a record for being in this state.
What it _does_ check is **lifecycle honesty**: does the declared status
accurately represent the record's real maturity, whatever that maturity is?
Marking something `approved`/`implemented` without any corroborating content
is a Fail; being honestly in `draft`/`planned` is not.

A later, separate maturity/review gate (not yet built) is what checks
whether a record is ready to move from `draft` to `approved`. That gate is
out of scope for this rubric.

## 3. Criterion Structure

Each criterion in a per-type rubric file has:

| Field       | Type       | Notes                                                                                                 |
| ----------- | ---------- | ----------------------------------------------------------------------------------------------------- |
| `id`        | string     | Stable, unique within the rubric (e.g. `P-001`). Never reused for a different meaning once published. |
| `dimension` | string     | Short human name for the criterion.                                                                   |
| `weight`    | float, 0–1 | All criteria weights in one rubric file sum to exactly `1.0`.                                         |
| `min_score` | float      | Fixed at `0` for every criterion in this system.                                                      |
| `max_score` | float      | Fixed at `2` for every criterion in this system — a uniform Fail/Partial/Pass scale.                  |
| `guidance`  | string     | The three anchor descriptions below.                                                                  |

### Scale (uniform across all criteria, all record types)

| Score | Label   | Meaning                                                         |
| ----- | ------- | --------------------------------------------------------------- |
| 2     | Pass    | Fully meets the criterion as described.                         |
| 1     | Partial | Present but incomplete, ambiguous, or requires inference.       |
| 0     | Fail    | Absent, or actively wrong (e.g. misrepresents lifecycle state). |

Each per-type rubric file gives a concrete Pass/Partial/Fail description
for every criterion — never just the bare labels — so a human author, a
generation process, or a future skill can score consistently without
guessing.

## 4. Aggregation

```text
overall_score = 100 * Σ(weight_i * score_i / max_score_i)
              = 100 * Σ(weight_i * score_i) / 2        # since max_score is always 2
```

`overall_score` is on a 0–100 scale, directly comparable across record
types and over time — the same normalization the platform Scorecard model
uses.

## 5. Pass Threshold

Each per-type rubric file declares its own `pass_threshold` (0–100). A
record is **ACCEPTED** only if `overall_score >= pass_threshold`; otherwise
it is **REJECTED** and excluded from the import/export JSON, the same
strict-mode discipline as the superseded rubric this one replaces.

Default `pass_threshold` for every record type in this system is **80**,
reflecting a compliance-grade bar. A per-type file may document a different
threshold if a future decision changes it — this file does not hardcode
type-specific values.

## 6. Rubric Governance

- These five per-type files, plus this one and `dataset-gates.md`, are the
  **source of truth** for authoring quality going forward — for the current
  automated generation process (#95) and for the future policy-editor skill
  alike. There is no older rubric to reconcile against; the prior draft
  (`test-data/rubrics/policy-standard-control-strict-rubrics.md`) has been
  retired.
- `policy-template.md`, `standard-template.md`, and `control-template.md`
  are drafting scaffolds paired with their rubrics — each template section
  maps 1:1 to a rubric criterion ID. A rubric criterion is never renamed,
  added, or removed without updating its matching template section in the
  same change; the two must not drift apart. (PracticeArea/RiskPath have no
  template yet — not requested.)
- Changing a criterion's `weight`, `guidance`, or a rubric's
  `pass_threshold` is a content change to this directory, reviewed like any
  other governance artifact — not a code change.
- Adding, removing, or renaming a criterion `id` is a breaking change to
  anything that stored a scorecard against the old `id` (once scorecards
  exist as persisted records, per the future policy-editor feature).
