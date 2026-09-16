<!-- © 2026 Cartman ApS. All rights reserved. -->

# Policy Authoring Rubric

**Status:** Source of truth
**Applies to:** `Policy` nodes (see `ps-domain-concepts.md#policy`)
**Scoring model:** See `scoring-model.md` for mechanics — this file lists
criteria only.
**Pass threshold:** 80 / 100

---

## Criteria

| ID    | Dimension                     | Weight | Pass (2)                                                                                                                                                                                                           | Partial (1)                                                                                         | Fail (0)                                                                                                                                                       |
| ----- | ----------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P-001 | Scope Clarity                 | 0.15   | `scope_in` and `scope_out` both state real boundaries for this Policy's commitment                                                                                                                                 | Only one of `scope_in`/`scope_out` is meaningfully populated                                        | Neither `scope_in` nor `scope_out` states a real boundary                                                                                                      |
| P-002 | Normative Language            | 0.15   | `normative_commitments` uses enforceable terms (`must`, `shall`, `required`) consistently for every commitment stated                                                                                              | `normative_commitments` mixes normative and descriptive/aspirational language                       | `normative_commitments` is purely descriptive or aspirational — nothing is actually mandated                                                                   |
| P-003 | Review Cadence                | 0.10   | `review_cadence` states an explicit interval or a concrete trigger condition for re-review                                                                                                                         | `review_cadence` says review happens but the interval/trigger is vague (e.g. "periodically")        | `review_cadence` empty or no trigger mentioned                                                                                                                 |
| P-004 | Exception Pathway             | 0.10   | `exception_pathway` names an explicit exception/risk-acceptance mechanism, including who can grant it                                                                                                              | `exception_pathway` mentions exceptions are possible without describing the mechanism               | `exception_pathway` empty — no exception pathway at all                                                                                                        |
| P-005 | Measurable Intent             | 0.15   | `measurable_outcomes` states at least one outcome that is quantifiable or otherwise objectively verifiable                                                                                                         | `measurable_outcomes` states an outcome but it is qualitative/subjective only                       | `measurable_outcomes` empty — no outcome stated                                                                                                                |
| P-006 | Capability Grouping Coherence | 0.20   | `capability_grouping_rationale` shows every governed Capability shares the same ownership model, review cadence, and control model — and this Policy was not minted merely because a new regulation article exists | Capabilities mostly cohere but at least one looks like a better fit for a different existing Policy | Groups Capabilities with genuinely different ownership/governance models, or is clearly "one Policy per regulation article"                                    |
| P-007 | Lifecycle Honesty             | 0.15   | `status` accurately reflects real maturity — `draft` for newly authored content, or `approved`/`deprecated` with the other properties actually supporting that state (e.g. `owner_id` present for `approved`)      | `status` is plausible but unverifiable from the other properties alone                              | `status` claims a maturity (e.g. `approved`) the other properties contradict (e.g. no `owner_id`, empty `scope_in`/`scope_out`, empty `normative_commitments`) |

**Weight sum check:** 0.15 + 0.15 + 0.10 + 0.10 + 0.15 + 0.20 + 0.15 = 1.00

---

## Rationale Notes

- P-006 replaces the superseded rubric's binary "governed by ≥1 Capability"
  hard-fail (structural — now covered by schema validation) with the actual
  _quality_ question: is this Policy minted for the right reason? This is
  the authoring-time enforcement of `ps-domain-concepts.md`'s minimality
  rule and #95's AC-BI-008/AC-BI-010.
- P-007 replaces the superseded rubric's `status == approved` hard-fail,
  which would have rejected every freshly authored record by design (see
  `scoring-model.md` §2). Honesty about lifecycle state, not a specific
  state, is what's scored.
- `version` (optional per schema) is deliberately not a separate criterion
  — an optional field's absence is not a quality defect.
- `scope_in`, `scope_out`, `normative_commitments`, `review_cadence`,
  `exception_pathway`, `measurable_outcomes`, and
  `capability_grouping_rationale` are real, dedicated properties on the
  `Policy` node (see `ps-domain-concepts.md#policy`), added specifically to
  pair 1:1 with these criteria and with `policy-template.md`'s sections —
  not free-form content buried inside `description`, which is now a short
  summary field only.
