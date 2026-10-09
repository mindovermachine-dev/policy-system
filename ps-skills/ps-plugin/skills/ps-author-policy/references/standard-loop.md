# Standard authoring loop (ps-author-policy)

Read on demand from `SKILL.md`'s Process once the Policy's `overall_score` clears its `pass_threshold`. Every Guardrail in `SKILL.md` still applies.

Reached once the Policy authoring loop's own `overall_score` clears its
`pass_threshold` (or, for "add another Standard", looped back into from
step 4 below under the same `policy_id`).

1. **Scaffold.** Derive a provisional title from the user's stated intent
   for this Standard and call `add-standard-to-draft(policy_id,
title=<derived>)` -- no `fields` at creation time, the same title-only
   creation shape as `create-policy-draft`. The new Standard is minted
   `status="draft"` (governance) and `implementation_status="draft"`
   (content) by the tool itself; neither is ever supplied by this skill.
2. **Field-by-field loop.** For each of S-001 Procedure Specificity, S-002
   Role Clarity, S-003 Boundary Clarity, S-004 Verification Readiness,
   S-005 Change Traceability, and S-006 Lifecycle Honesty
   (`standard-rubric.md`'s own listed order, or starting from whichever is
   weakest-scoring on a resumed Standard), ask exactly one Socratic
   question targeting that criterion's own Pass/Partial/Fail description
   (`standard-template.md`'s matching section). Immediately before drafting
   S-001's `procedure` or S-004's `verification_notes` specifically --
   both "how" content -- follow the "Web research and citations" note
   below. On the user's answer, call
   `update-standard-draft(standard_id, fields={<property>: <answer>})`
   immediately -- persisting before re-scoring or asking the next
   question. `procedure`, `applicability_boundary`, `verification_notes`,
   `change_rationale`, and `implementation_status` are each one property
   patched by their own call; S-002 Role Clarity is the one criterion with
   two properties (`implementer_role` and `reviewer_role`) -- both
   answered by a single question ("who implements this, and who reviews
   it?") and persisted together in one call, the same one-criterion/
   compound-field pattern the Policy loop's own P-001 scaffold used for
   `scope_in`/`scope_out`.
3. **S-006 Lifecycle Honesty is asked about, unlike Policy's P-007.**
   `implementation_status` is a genuine content property of Standard
   (distinct from its own immutable governance `status`, which this skill
   never touches) and is patchable through `update-standard-draft` -- so,
   unlike the Policy loop's P-007 (which scores the immutable `status` and
   is never asked about), S-006 gets its own Socratic question like every
   other S-00x criterion. The honest answer while this skill is authoring
   is almost always `"draft"` (matching the governance `status`), but the
   question is still asked and the answer still persisted, since
   `scoring-model.md`'s own Lifecycle Honesty check is about whether the
   _stated_ maturity matches the other properties, not merely about
   restating the default.
4. **Immediate-persist-then-rescore.** After each field is persisted, re-
   read the Standard's six content properties (`procedure`,
   `implementer_role`, `reviewer_role`, `applicability_boundary`,
   `verification_notes`, `change_rationale`) plus `implementation_status`
   via `cypher` and compute `overall_score` per `scoring-model.md` §4
   (`100 * Σ(weight_i * score_i) / 2`) against `standard-rubric.md`'s own
   six weights (S-001..S-006) -- never a different rubric's weights, never
   an ad hoc score.
5. **Stop condition.** Once `overall_score >= 80` (`standard-rubric.md`'s
   own `pass_threshold`), report this Standard as passing and stop
   iterating its field loop, even if a lower-weight S-00x criterion is
   still Partial or Fail.
6. **Cardinality question.** Once this Standard's own rubric passes, ask
   exactly one question -- never a pre-asked target count: add another
   Standard, move to Controls, or finish. Nothing is inferred or assumed;
   whichever the user answers is the only branch taken.
   - **Add another Standard** -- loop back to step 1 under the same
     `policy_id`, scaffolding a second Standard.
   - **Move to Controls** -- hand off to the Control authoring loop's own
     step 1 for this Standard.
   - **Finish** -- hand off to Session end below; no further tool call is
     made here.
