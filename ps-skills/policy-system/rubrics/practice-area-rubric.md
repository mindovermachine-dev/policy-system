<!-- © 2026 Cartman ApS. All rights reserved. -->

# PracticeArea Authoring Rubric

**Status:** Source of truth
**Applies to:** `PracticeArea` nodes (see `ps-domain-concepts.md#practicearea`)
**Scoring model:** See `scoring-model.md` for mechanics — this file lists
criteria only.
**Pass threshold:** 80 / 100

---

## Criteria

| ID     | Dimension                     | Weight | Pass (2)                                                                                                                                                                                      | Partial (1)                                                                                       | Fail (0)                                                                                                        |
| ------ | ----------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| PA-001 | Naming Stability              | 0.20   | Name is a stable, regulation-independent engineering discipline label (e.g. "Secure Development Lifecycle") that would still make sense years from now, regardless of which regulations exist | Name is understandable but tied to a current tool, team name, or regulation                       | Name is effectively a regulation article title or a tool name in disguise                                       |
| PA-002 | Scope Breadth                 | 0.25   | Groups a coherent, recognizable engineering discipline — neither a single team/tool ("Jira Configuration") nor the entire SDLC in one bucket                                                  | Scope is workable but noticeably too narrow or too broad compared to peer areas                   | Scope is unusable — either far too narrow to justify its own area, or so broad it would absorb most other areas |
| PA-003 | Capability Coverage Coherence | 0.25   | Every Capability this area will `COVERS` is a clear, uncontested member of the named discipline                                                                                               | Most Capabilities fit; at least one is a stretch                                                  | Capabilities span genuinely unrelated disciplines                                                               |
| PA-004 | Ownership Assignability       | 0.15   | Described clearly enough that a single accountable practice owner could be named for it, supporting the Policies it will `OWNS`                                                               | Ownership is plausible but would require splitting responsibility across teams with no clear lead | No plausible single owner — ownership is diffuse or undefined                                                   |
| PA-005 | Lifecycle Honesty             | 0.15   | `status` (`active`/`deprecated`) accurately reflects real current use                                                                                                                         | Status is plausible but unverifiable from content alone                                           | Status contradicts content (e.g. `deprecated` area still described as the current baseline)                     |

**Weight sum check:** 0.20 + 0.25 + 0.25 + 0.15 + 0.15 = 1.00

---

## Rationale Notes

- This record type had no per-record criteria in the superseded rubric — it
  only appeared in dataset-level gates. PA-002 and PA-003 directly encode
  the guideline's documented failure modes ("practice areas that are too
  narrow") and the 10-area baseline shape from
  `minimal-engineering-policy-seed-guideline.md`.
