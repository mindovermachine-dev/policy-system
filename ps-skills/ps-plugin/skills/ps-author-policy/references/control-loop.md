# Control authoring loop (ps-author-policy)

Read on demand from `SKILL.md`'s Process once a Standard's rubric passes and the user chooses to move to Controls. Every Guardrail in `SKILL.md` still applies.

Reached once a Standard's own rubric passes and the user chooses "move to
Controls" (Standard loop's own cardinality question), or looped back into
from step 4 below under the same `standard_id`.

1. **Scaffold.** Derive a provisional title from the user's stated intent
   for this Control and ask, as part of this Control's first question,
   whether it is `automated` or `manual` (`control_type`). Call
   `add-control-to-draft(standard_id, title=<derived>,
control_type=<answer>)` -- no `fields` at creation time, the same
   title-only creation shape as `create-policy-draft`/
   `add-standard-to-draft`. The new Control is minted governance
   `status="draft"` and content `implementation_status="planned"` by the
   tool itself -- **`"planned"`, not `"draft"`**: Control's own workflow
   starts one step later than Standard's (`ps-domain-concepts.md`'s
   "earliest state in status workflow" convention for Control). Never
   describe a freshly scaffolded Control's `implementation_status` as
   `"draft"` in any question, report, or summary -- that word names only
   the separate, immutable governance `status`, which this skill never
   sets and never asks about.
2. **Field-by-field loop.** For each of C-001 Pass/Fail Objectivity, C-002
   Execution Clarity, C-003 Evidence Path Defined, C-004 Ownership Clarity,
   C-005 Risk Alignment, and C-006 Lifecycle Honesty
   (`control-rubric.md`'s own listed order, or starting from whichever is
   weakest-scoring on a resumed Control), ask exactly one Socratic question
   targeting that criterion's own Pass/Partial/Fail description
   (`control-template.md`'s matching section). Immediately before drafting
   C-002's `execution_method` specifically -- "how" content -- follow the
   "Web research and citations" note below. On the user's answer, call
   `update-control-draft(control_id, fields={<property>: <answer>})`
   immediately -- persisting before re-scoring or asking the next question.
   `pass_fail_criteria`, `execution_method`, `evidence_plan`, and
   `risk_alignment_rationale` are each one property patched by their own
   call; `implementation_status` is C-006's own property, asked about and
   persisted the same way (the honest answer while this skill is authoring
   is almost always `"planned"`, matching the server's own default, but the
   question is still asked and the answer still persisted -- same
   Lifecycle-Honesty discipline as the Standard loop's own S-006, never
   merely restating the default unasked). C-004 Ownership Clarity is the
   one criterion with two properties (`executor_role` and `reviewer_role`)
   -- both answered by a single question ("who executes this Control, and
   who reviews the result?") and persisted together in one call, the same
   one-criterion/compound-field pattern the Policy loop's P-001 and the
   Standard loop's S-002 already used.
3. **`control_type` is patchable after creation too, unlike at creation
   time.** `add-control-to-draft`'s own `fields` parameter excludes
   `"type"` -- `control_type` is the only way to set it when the Control is
   minted. But `update-control-draft`'s `fields` allow-list does include
   `"type"` -- if the user wants to change `automated`/`manual` after
   creation, patch it with `update-control-draft(control_id,
fields={"type": <answer>})`, the only post-creation path to change it.
4. **Immediate-persist-then-rescore.** After each field is persisted, re-
   read the Control's content properties (`pass_fail_criteria`,
   `execution_method`, `evidence_plan`, `executor_role`, `reviewer_role`,
   `risk_alignment_rationale`, `implementation_status`) via `cypher` and
   compute `overall_score` per `scoring-model.md` §4
   (`100 * Σ(weight_i * score_i) / 2`) against `control-rubric.md`'s own
   six weights (C-001..C-006) -- never a different rubric's weights, never
   an ad hoc score.
5. **Stop condition.** Once `overall_score >= 80` (`control-rubric.md`'s
   own `pass_threshold`), report this Control as passing and stop iterating
   its field loop, even if a lower-weight C-00x criterion is still Partial
   or Fail.
6. **Cardinality question.** Once this Control's own rubric passes, **or
   the user declines to add a Control to this Standard at all**, ask
   exactly one question -- never a pre-asked target count: add another
   Control, add another Standard, or finish. A Standard can legitimately
   have zero Controls (the schema's own cardinality requires exactly one
   `IMPLEMENTED_BY` inbound edge _per Control_ that exists, never a minimum
   count _per Standard_) -- declining is never treated as an incomplete or
   abandoned step, and leads to the same three-way question a passing
   Control would.
   - **Add another Control** -- loop back to step 1 under the same
     `standard_id`, scaffolding a second Control.
   - **Add another Standard** -- hand back to the Standard authoring
     loop's own step 1 under the same `policy_id`.
   - **Finish** -- hand off to Session end below; no further tool call is
     made here.
