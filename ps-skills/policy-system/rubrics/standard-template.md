<!-- © 2026 Cartman ApS. All rights reserved. -->

# Standard Authoring Template

**Status:** Source of truth
**Applies to:** `Standard` nodes — pair with `standard-rubric.md` (criterion
IDs below map 1:1 to that file) and `scoring-model.md`. Each section below is
a real, named property on the `Standard` node (see
`ps-domain-concepts.md#standard`) — transcribe this template's content
directly into the matching property key in the internal-seed intake
document, not into `description`.

Fill in every section below. Leave nothing as the italic instruction text —
replace it with real content, or explicitly write "N/A" with a one-line
reason if a section genuinely doesn't apply.

---

## Standard Title

**Property:** `title`

## Description

**Property:** `description`

_Short human-readable summary only — not a catch-all. The substantive
content goes in the structured properties below, each in its own field._

## Supports Policy

_Name the exactly-one parent Policy this Standard supports (`SUPPORTED_BY`
edge — not a node property). A Standard supports one Policy only — if this
content is really about a different Policy, split it into a separate
Standard._

## Procedure

**Property:** `procedure`

_Write steps explicit enough that two different implementers would execute
them the same way — not a restatement of the parent Policy's intent._

→ Scores **S-001 Procedure Specificity**.

## Roles

**Properties:** `implementer_role`, `reviewer_role`

_Implementer (`implementer_role`):_ ...
_Reviewer (`reviewer_role`):_ ...

→ Scores **S-002 Role Clarity**. (Note: `Standard` has no `owner_id`
property — these two fields are the only place this lives.)

## Applicability Boundary

**Property:** `applicability_boundary`

_State which systems, environments, or data classes this Standard governs
— and, as important, which it does not._

→ Scores **S-003 Boundary Clarity**.

## Verification Design Notes

**Property:** `verification_notes`

_Write this procedure so a Control could be built directly against it,
pass/fail, without the Control author having to guess or interpret. If you
can't picture what a Control checking this would actually test, the
procedure isn't specific enough yet — go back and tighten it._

→ Scores **S-004 Verification Readiness**.

## Change Rationale

**Property:** `change_rationale`

_For a new Standard: state the reason it's being introduced now. For a
revision: state what changed from the previous version and why._

→ Scores **S-005 Change Traceability**.

## Status

**Property:** `implementation_status`

- `implementation_status`: `draft` | `implemented` | `reviewed` | `deprecated`

_If this is other than `draft`, the content above must actually support
that — `implemented` means the procedure is genuinely being followed, not
merely written._

→ Scores **S-006 Lifecycle Honesty**.

## Version

**Property:** `version`

_Optional. Free text, e.g. `1.0`._

---

## Rubric Self-Check

Before submitting, self-score each criterion (`Pass` / `Partial` / `Fail`)
per `standard-rubric.md`. This is a drafting aid, not the authoritative
score — the rubric itself is what gates the record.

- S-001 Procedure Specificity:
- S-002 Role Clarity:
- S-003 Boundary Clarity:
- S-004 Verification Readiness:
- S-005 Change Traceability:
- S-006 Lifecycle Honesty:
