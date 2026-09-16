<!-- © 2026 Cartman ApS. All rights reserved. -->

# Control Authoring Template

**Status:** Source of truth
**Applies to:** `Control` nodes — pair with `control-rubric.md` (criterion
IDs below map 1:1 to that file) and `scoring-model.md`. Each section below is
a real, named property on the `Control` node (see
`ps-domain-concepts.md#control`) — transcribe this template's content
directly into the matching property key in the internal-seed intake
document, not into `description`.

Fill in every section below. Leave nothing as the italic instruction text —
replace it with real content, or explicitly write "N/A" with a one-line
reason if a section genuinely doesn't apply.

---

## Control Title

**Property:** `title`

## Description

**Property:** `description`

_Short human-readable summary only — not a catch-all. The substantive
content goes in the structured properties below, each in its own field._

## Verifies Standard

_Name the exactly-one parent Standard this Control verifies
(`IMPLEMENTED_BY` edge — not a node property). A Control verifies one
Standard only._

## Type

**Property:** `type`

- `type`: `automated` | `manual`

## Verifies Risk Path(s)

_List every RiskPath this Control will be reachable from via
`VERIFIED_BY` (edge, not a node property). A Control can serve more than
one RiskPath._

## Pass/Fail Criteria

**Property:** `pass_fail_criteria`

_Define success precisely enough that two different executors would reach
the same verdict — not "check that it works."_

→ Scores **C-001 Pass/Fail Objectivity**.

## Execution Method and Intended Frequency

**Property:** `execution_method`

_Describe the method. `execution_frequency` is a separate, legitimately
null property at authoring time for a `planned` Control — state the
intended trigger/cadence here in prose even before that structured field
is set._

→ Scores **C-002 Execution Clarity**.

## Evidence Plan

**Property:** `evidence_plan`

_`evidence_ref` is a separate, legitimately null property at authoring
time — state here what evidence this Control will produce and where it's
expected to live (log system, ticket, artifact store) so it's clear what
"evidence" will mean once this Control executes._

→ Scores **C-003 Evidence Path Defined**.

## Ownership

**Properties:** `executor_role`, `reviewer_role`

_Executor (`executor_role`):_ ...
_Reviewer (`reviewer_role`):_ ...

→ Scores **C-004 Ownership Clarity**. (Note: `Control` has no `owner_id`
property — these two fields are the only place this lives.)

## Risk Alignment Rationale

**Property:** `risk_alignment_rationale`

_Explain specifically how this Control's objective addresses the risk
exposure of the RiskPath(s) listed above — not just a topical connection._

→ Scores **C-005 Risk Alignment**.

## Status

**Property:** `implementation_status`

- `implementation_status`: `planned` | `implemented` | `reviewed` | `deprecated`

_If this is other than `planned`, the content above must actually support
that — `implemented` needs real execution history (`last_test_date`,
`evidence_ref`), not just the label._

→ Scores **C-006 Lifecycle Honesty**.

## Operational Fields (leave blank at authoring time)

**Properties:** `execution_frequency`, `last_test_date`, `next_review_date`,
`evidence_ref`

- `execution_frequency`: _(filled in once scheduled)_
- `last_test_date`: _(filled in after first execution)_
- `next_review_date`: _(filled in once scheduled)_
- `evidence_ref`: _(filled in once evidence exists)_

---

## Rubric Self-Check

Before submitting, self-score each criterion (`Pass` / `Partial` / `Fail`)
per `control-rubric.md`. This is a drafting aid, not the authoritative
score — the rubric itself is what gates the record.

- C-001 Pass/Fail Objectivity:
- C-002 Execution Clarity:
- C-003 Evidence Path Defined:
- C-004 Ownership Clarity:
- C-005 Risk Alignment:
- C-006 Lifecycle Honesty:
