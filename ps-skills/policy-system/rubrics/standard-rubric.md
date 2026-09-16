<!-- © 2026 Cartman ApS. All rights reserved. -->

# Standard Authoring Rubric

**Status:** Source of truth
**Applies to:** `Standard` nodes (see `ps-domain-concepts.md#standard`)
**Scoring model:** See `scoring-model.md` for mechanics — this file lists
criteria only.
**Pass threshold:** 80 / 100

---

## Criteria

| ID    | Dimension              | Weight | Pass (2)                                                                                                                                                                                                                                | Partial (1)                                                                               | Fail (0)                                                                                                     |
| ----- | ---------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| S-001 | Procedure Specificity  | 0.20   | `procedure` is explicit and concrete enough that two different implementers would execute it the same way                                                                                                                               | `procedure` is present but leaves meaningful room for interpretation                      | `procedure` empty, or restates the parent Policy's intent without adding "how"                               |
| S-002 | Role Clarity           | 0.15   | `implementer_role` and `reviewer_role` are both distinctly stated                                                                                                                                                                       | One of `implementer_role`/`reviewer_role` is stated, the other isn't                      | Both empty — no role information                                                                             |
| S-003 | Boundary Clarity       | 0.15   | `applicability_boundary` states which systems, environments, or data classes this Standard governs                                                                                                                                      | `applicability_boundary` is partial (e.g. system named, environment not)                  | `applicability_boundary` empty — reads as universally applicable with no scoping                             |
| S-004 | Verification Readiness | 0.20   | `verification_notes` (read together with `procedure`) is specific enough that a Control could directly test conformance, pass/fail, without further interpretation                                                                      | Verifiable in principle but would require the Control author to make interpretive choices | Not verifiable as written — no Control could objectively test conformance                                    |
| S-005 | Change Traceability    | 0.10   | `change_rationale` states a revision rationale or change marker distinguishing this version from its predecessor                                                                                                                        | `version` is set but `change_rationale` doesn't say what changed or why                   | Both empty — no versioning or change information at all                                                      |
| S-006 | Lifecycle Honesty      | 0.20   | `implementation_status` accurately reflects real maturity — `draft` for newly authored content, or a later state supported by the other properties (e.g. `implemented` only once `procedure` is genuinely followed, not merely written) | Status is plausible but unverifiable from the other properties alone                      | Status claims a maturity (`implemented`/`reviewed`) the other properties contradict (e.g. empty `procedure`) |

**Weight sum check:** 0.20 + 0.15 + 0.15 + 0.20 + 0.10 + 0.20 = 1.00

---

## Rationale Notes

- S-004 replaces the superseded rubric's structural "verification readiness"
  scored check with an explicit, testable definition: could a Control
  actually be written against this text as-is?
- S-006 replaces the superseded rubric's `implementation_status ∈
{implemented, reviewed}` hard-fail, which would have rejected every
  freshly authored Standard (default `draft`, per `ps-domain-concepts.md`
  and `scoring-model.md` §2).
- `procedure`, `implementer_role`, `reviewer_role`, `applicability_boundary`,
  `verification_notes`, and `change_rationale` are real, dedicated
  properties on the `Standard` node (see `ps-domain-concepts.md#standard`),
  added specifically to pair 1:1 with these criteria and with
  `standard-template.md`'s sections — not free-form content buried inside
  `description`, which is now a short summary field only.
