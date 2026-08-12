<!-- © 2026 Cartman ApS. All rights reserved. -->
# Narrowed-question fixtures

Structured `Question`/`Entities`/`Edges` artifacts produced by
`.github/skills/policy-question/SKILL.md` steps 1-4 (Socratic narrowing +
user approval), persisted here so later smoke tests can reuse them without
re-running the narrowing dialogue each time. Each fixture records the exact
skill version it was produced under and the user-approval context, since
steps 1-4's wording (and therefore its output) can change between skill
revisions — a fixture produced under one version isn't guaranteed to be
what a later version would produce from the same open question.

These are inputs for isolated tests of steps 5+ (freehand retrieval, answer
construction, and later increments) — not answers, not fitness functions.

## NQ-001 — CRA active-version Roles

- **Produced by:** `policy-question` v0.1.0 (pre-retrieval revision), narrowed and approved interactively 2026-08-12.
- **Origin:** follow-up question after narrowing "Are we CRA compliant?" ("How many roles are specified in the active CRA regulation?"), then adjusted once by the user to also request role names.

```
Question: How many distinct Role nodes are defined (via DEFINES) by the active version of the CRA regulation, and what are their names?

Entities: Regulation, Role
Edges: DEFINES
```

## NQ-002 — CRA active-version implementation-chain gaps

- **Produced by:** `policy-question` v0.1.0 (pre-retrieval revision), narrowed and approved interactively 2026-08-12.
- **Origin:** narrowed from the open question "Are we CRA compliant?", refined toward "I need to determine if there is anything we need to do as an organization to be compliant" — gap analysis over missing links and incomplete implementation status, scoped to the active CRA version only.

```
Question: For the active version of CRA, which Requirements do not have a complete implementation chain — i.e., where the trace from Requirement → Obligation → Capability → Policy → Standard → Control is missing a link, or reaches a Policy that isn't approved, a Standard that isn't implemented/reviewed, or a Control that isn't implemented/reviewed?

Entities: Regulation, Requirement, Obligation, Capability, Policy, Standard, Control
Edges: EXPRESSES, SATISFIED_BY, REQUIRES, GOVERNED_BY, SUPPORTED_BY, IMPLEMENTED_BY
```
