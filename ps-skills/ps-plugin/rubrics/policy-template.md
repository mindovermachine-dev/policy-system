<!-- © 2026 Cartman ApS. All rights reserved. -->

# Policy Authoring Template

**Status:** Source of truth
**Applies to:** `Policy` nodes — pair with `policy-rubric.md` (criterion IDs
below map 1:1 to that file) and `scoring-model.md`. Each section below is a
real, named property on the `Policy` node (see `ps-domain-concepts.md#policy`)
— transcribe this template's content directly into the matching property key
in the internal-seed intake document, not into `description`.

Fill in every section below. Leave nothing as the italic instruction text —
replace it with real content, or explicitly write "N/A" with a one-line
reason if a section genuinely doesn't apply.

---

## Policy Title

**Property:** `title`

_Short, names the commitment — not the regulation it originated from._

## Description

**Property:** `description`

_Short human-readable summary only — not a catch-all. The substantive
content goes in the structured properties below, each in its own field._

## Governed Capabilities

_List every Capability this Policy will govern (one `GOVERNED_BY` edge per
Capability, inbound to this Policy — not a node property). Confirm each
shares the same ownership model, review cadence, and control model — see
Capability Grouping Rationale below._

## Scope

**Properties:** `scope_in`, `scope_out`

_In-scope (`scope_in`):_ ...
_Out-of-scope (`scope_out`):_ ...

→ Scores **P-001 Scope Clarity**.

## Normative Commitments

**Property:** `normative_commitments`

_State each commitment using enforceable language (`must`, `shall`,
`required`) — not descriptive or aspirational language._

→ Scores **P-002 Normative Language**.

## Review Cadence

**Property:** `review_cadence`

_State an explicit review interval (e.g. "annually") or a concrete trigger
condition (e.g. "on every major cloud provider change") — not "periodically."_

→ Scores **P-003 Review Cadence**.

## Exception Pathway

**Property:** `exception_pathway`

_Name the exception/risk-acceptance mechanism and who can grant it._

→ Scores **P-004 Exception Pathway**.

## Measurable Outcomes

**Property:** `measurable_outcomes`

_State at least one outcome that is quantifiable or otherwise objectively
verifiable — not purely qualitative._

→ Scores **P-005 Measurable Intent**.

## Capability Grouping Rationale

**Property:** `capability_grouping_rationale`

_Explain why the Capabilities listed above belong together under one
Policy: same owner, same governance cadence, same control model. If this
Policy exists mainly because a specific regulation article exists, that is
a Fail on this criterion — map to an existing Policy instead (see the
minimality rule in `ps-domain-concepts.md`)._

→ Scores **P-006 Capability Grouping Coherence**.

## Ownership and Status

**Properties:** `owner_id`, `status`

- `owner_id`: ...
- `status`: `draft` | `approved` | `deprecated`

_If `status` is anything other than `draft`, the content above must
actually support that — e.g. `approved` needs a real owner and a
defensible review history, not just the label._

→ Scores **P-007 Lifecycle Honesty**.

## Version

**Property:** `version`

_Optional. Free text, e.g. `1.0`._

---

## Rubric Self-Check

Before submitting, self-score each criterion (`Pass` / `Partial` / `Fail`)
per `policy-rubric.md`. This is a drafting aid, not the authoritative
score — the rubric itself is what gates the record.

- P-001 Scope Clarity:
- P-002 Normative Language:
- P-003 Review Cadence:
- P-004 Exception Pathway:
- P-005 Measurable Intent:
- P-006 Capability Grouping Coherence:
- P-007 Lifecycle Honesty:
