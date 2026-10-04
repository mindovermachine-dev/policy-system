---
name: ps-author-policy
description: Guide a user through authoring a new or amended Policy/Standard/Control tree in the Policy System compliance graph -- detecting whether a governing Policy already exists for the named Capability(ies), then walking Policy -> Standard(s) -> Control(s) one rubric criterion at a time until each document's own rubric-weighted score clears its pass_threshold.
---

# ps-author-policy

## Purpose

Drive a user through authoring the Policy/Standard/Control content that
closes a Capability's coverage gap, one rubric criterion at a time, using
issue #136's content-CRUD MCP tools. Given one or more Capability names,
first detects whether a governing Policy already exists (none -> fresh
top-down draft; the caller's own Draft -> resume it at its weakest-scoring
criterion; a Proposed or Approved Policy with no superseding fork yet ->
fork a superseding draft via `supersedes_policy_id`; one that already has
a fork -> resume the caller's own draft fork, or stop on a named state),
then walks
Policy -> Standard(s) -> Control(s), asking exactly one Socratic question
per weak rubric criterion, persisting each answered field immediately,
offering -- never requiring -- cited web research for "how" content, and
stopping iteration on each document once its own rubric-weighted
`overall_score` clears its `pass_threshold`. Never calls `propose-policy`,
`approve-policy`, `reject-policy`, or `revert-policy-to-draft` on its own
initiative -- the resulting tree is always left ready for the user's own
separate, later request to trigger `propose-policy`.

**Scope:** authors Policy/Standard/Control content only -- never
PracticeArea/RiskPath authoring, never a dataset-level completeness gate
(`dataset-gates.md`), never a status-transition tool call.

**Deliverable:** a scaffolded, resumed, or forked Policy with as many
Standards/Controls as the user chooses to add, each field persisted to the
graph immediately as it is answered, plus a session-end summary of every
document's rubric score and cascade-coherence state.

## On Load

Exactly one connector name is recognised: `ps-mcp` ("Policy System MCP"),
the connector the Policy System Plugin declares, pointing at a hosted PS
Service. Claude lists it as `plugin:ps-plugin:ps-mcp`. Any other name is not
a PS Service connector. A connector
that is present under the right name but does not expose a
`create-policy-draft` tool is **not** a PS Service connector either,
whatever it is named -- report it as unreachable rather than proceeding
against it. From here on, "the PS Service connector" means the one
selected here.

(This convention mirrors `ps-ingest-regulation/SKILL.md`'s own On Load
section verbatim, lines 28-46, adapted only to this skill's own signature
tool, `create-policy-draft`, in place of `ingest_regulation`.)

Once the connector is selected, call the `domain_concepts` tool once per
session (same On-Load-time convention `ps-qna/SKILL.md` already uses) to
ground this session's own vocabulary against `ps-domain-concepts.md`'s
entities/relationships -- not required again per call, only once at load.

## Core Principles

- Never proceed without at least one named Capability -- branch detection
  cannot begin without it, and no tool call is made until one is given.
- A named Capability's existence, and its `GOVERNED_BY` state, are always
  confirmed via `cypher` before any content-CRUD tool call is attempted --
  never assumed, never guessed from a prior session.
- There is no "who am I" tool on the PS Service connector. For a governing
  Policy currently `"draft"`, `get-policy`'s own access gate
  (owner-or-`SystemOwner`/`SystemAdmin`) _is_ the identity check: a
  successful `get-policy` call means the Draft is resumable; a
  `PolicyDraftAccessDeniedError` means it is not, and is never treated as
  a signal to fork it or to create a competing Policy instead.
- A governing Policy that is currently `"proposed"` or `"approved"` and has
  no `SUPERSEDED_BY` successor is always forked immediately
  (`create-policy-draft` with `supersedes_policy_id`) -- ownership of a
  Proposed/Approved Policy is irrelevant to which action is taken, because
  none of this skill's authoring tools can resume a Policy that isn't
  itself `"draft"`. When a successor already exists, the skill never
  creates a second fork (see Branch detection). When the
  prior was only `"proposed"` (not yet `"approved"`), the fork attempt
  itself is rejected by the tool with a named error, which is reported
  plainly -- never treated as if the fork had been silently blocked before
  it was attempted.
- If two or more named Capabilities' `GOVERNED_BY` lookups disagree (e.g.
  one Capability has no governing Policy while another is already governed
  by a Draft or a Proposed/Approved Policy), report the disagreement
  plainly to the user and ask how to proceed (which Capability's situation
  to follow, or whether to split the request into separate sessions) --
  never guess or silently pick one Capability's branch. No
  `create-policy-draft`, `get-policy`, or any other content-CRUD tool call
  is made until the user resolves the disagreement.
- Every persisted field update happens immediately after the user answers
  it, before re-scoring or asking the next question -- never batched,
  never deferred.
- Never fabricate a rubric score, a tool result, or a persisted field
  value -- report exactly what each tool call returned.
- Never call `propose-policy`, `approve-policy`, `reject-policy`, or
  `revert-policy-to-draft` -- this skill only ever authors content; every
  status transition is always the user's own separate, later request.

## Process

### Branch detection

1. Ask the user to name one or more Capabilities this session concerns;
   do not proceed until at least one is given.
2. For each named Capability, confirm it exists: call `cypher` with

   ```cypher
   MATCH (c:Capability {name: '<name>'})
   RETURN c.name AS capability_name, c.id AS capability_id
   ```

   A Capability with zero returned rows is reported as not found and
   dropped from the rest of this flow -- never attempt to create a Policy
   against it.

3. For each Capability that does exist, check whether it already has a
   governing Policy: call `cypher` with

   ```cypher
   MATCH (c:Capability {name: '<name>'})-[:GOVERNED_BY]->(p:Policy)
   OPTIONAL MATCH (p)-[:SUPERSEDED_BY]->(f:Policy)
   RETURN p.id AS policy_id, p.status AS status, f.id AS fork_id, f.status AS fork_status
   ```

   `fork_id`/`fork_status` are the Policy's `SUPERSEDED_BY` successor(s)
   (null when none). More than one row per Capability is possible when
   several forks exist; handle them all as described below. A fork keeps
   the Capability's `GOVERNED_BY` edge on the prior Policy until the fork
   is approved, so an in-progress fork is only visible through
   `fork_id`/`fork_status`.

4. If the named Capabilities' results disagree with each other (Core
   Principles), stop here: report the disagreement and ask the user how to
   proceed. Otherwise, branch on the single agreed-upon situation:
   - **No `GOVERNED_BY` row for any named Capability** -- fresh branch: no
     governing Policy exists yet for this Capability (or set of
     Capabilities). Hand off to the Policy field-by-field authoring loop,
     which scaffolds a brand-new Policy.
   - **A row with `status == "draft"`** -- call `get-policy` with that
     `policy_id`.
     - Success -- the caller owns this Draft (or holds `SystemOwner`/
       `SystemAdmin`): resume it, opening the field loop at its
       weakest-scoring rubric criterion.
     - `error: you do not have access to this Policy` -- this Draft
       belongs to someone else and is not forkable (only an `"approved"`
       prior can be forked). Report this as a distinct
       `governed_by_unowned_draft` state and stop -- never create a
       second, competing Policy for the same Capability.
   - **A row with `status` of `"proposed"` or `"approved"` and a non-null
     `fork_id`** -- a fork already exists; never call `create-policy-draft`
     again. Examine every successor row, in this order:
     - A successor with `fork_status == "draft"`: call `get-policy` with
       its `fork_id`. Success -- the caller owns the fork: resume it,
       opening the field loop at its weakest-scoring rubric criterion.
       `error: you do not have access to this Policy` -- the fork belongs
       to someone else. Report the distinct `superseded_by_unowned_draft`
       state and stop. (If another successor row is an owned draft,
       resume that one instead.)
     - Otherwise, a successor with `fork_status == "proposed"`: report the
       distinct `fork_awaiting_approval` state and stop -- the fork awaits
       the user's own separate approval, which this skill never triggers.
     - Otherwise (every successor is `"approved"`, rejected or otherwise
       not in progress): fork as in the next bullet.
   - **A row with `status` of `"proposed"` or `"approved"` and no
     `fork_id`** -- fork
     branch: immediately call `create-policy-draft` with
     `title=<derived from the Capability name(s)>` and
     `supersedes_policy_id=<policy_id>`. This is always attempted, never
     pre-emptively blocked.
     - Success -- the forked draft's own Standard/Control children
       (forked from the prior) already exist; open the field loop at the
       forked content's weakest-scoring criterion, same as resume.
     - `error: Policy '<policy_id>' cannot be superseded: current status
is 'proposed', requires 'approved'` -- the prior was only
       Proposed, not yet Approved. Report this named state and stop.

### Policy authoring loop

Reached from the branch-detection sub-flow's fresh branch (a brand-new
Policy), or opened directly at the weakest-scoring criterion for a resume
or fork continuation (the same loop, just not started at the top).

1. **Scaffold.** `create-policy-draft` has no content-field parameter at
   all -- only `title` -- so the scaffold is two calls, not one:
   1. Derive a provisional title from the named Capability(ies) (e.g.
      `"{Capability name} Policy"` for one Capability; for several, a
      name for the shared grouping) and call
      `create-policy-draft(title=<derived>, capability_ids=[<capability_id
of every Capability found by the step 2 existence check>])`. The
      new draft claims those Capabilities (`GOVERNED_BY` is written at
      creation), so the next branch detection finds it as their governing
      Policy with `status == "draft"` and resumes it. Pass only ids step 2
      returned, never ids for dropped (not found) Capabilities, and never
      pass `capability_ids` together with `supersedes_policy_id` -- a fork
      does not claim Capabilities (they move to it when the fork is
      approved). `title` can never be
      patched through `update-policy-draft` afterwards -- if the user
      wants a materially different title later, the only path is
      abandoning this draft and starting a new one (see Guardrails).
   2. Immediately call `update-policy-draft(policy_id, fields={"scope_in":
<provisional>, "scope_out": <provisional>})` with a provisional
      P-001 Scope Clarity value the skill drafts itself -- not yet a user
      answer. Display the title together with these two fields to the
      user now, before asking any question.
2. **Field-by-field loop.** For each of P-002 Normative Language, P-003
   Review Cadence, P-004 Exception Pathway, P-005 Measurable Intent, and
   P-006 Capability Grouping Coherence (`policy-rubric.md`'s own listed
   order, or starting from whichever of these is weakest-scoring on a
   resume/fork continuation), ask exactly one Socratic question targeting
   that criterion's own Pass/Partial/Fail description
   (`policy-template.md`'s matching section). On the user's answer, call
   `update-policy-draft(policy_id, fields={<property>: <answer>})`
   immediately -- persisting that one property before re-scoring or
   asking the next question. `normative_commitments`, `review_cadence`,
   `exception_pathway`, `measurable_outcomes`, and
   `capability_grouping_rationale` are the five properties this step
   patches, one per call, never batched.
3. **P-007 Lifecycle Honesty is never asked about here.** It scores the
   Policy's own `status`, which `update-policy-draft` can never patch
   (immutable, same as `title`) and which this skill never changes (no
   status-transition tool is ever called). A Policy scaffolded by this
   loop stays honestly `"draft"` throughout -- `scoring-model.md`'s own
   authoring-time-gate rule is that being honestly in `draft` is never a
   Fail -- so P-007 is inherently satisfied for as long as this skill is
   the one authoring the record, with no field to persist and no question
   to ask.
4. **Immediate-persist-then-rescore.** After each field is persisted (step
   1.2 and every iteration of step 2), re-read the Policy's seven content
   properties (`scope_in`, `scope_out`, `normative_commitments`,
   `review_cadence`, `exception_pathway`, `measurable_outcomes`,
   `capability_grouping_rationale`) via `cypher` and compute
   `overall_score` per `scoring-model.md` §4
   (`100 * Σ(weight_i * score_i) / 2`) against `policy-rubric.md`'s own
   seven weights (P-001..P-007, including P-007's fixed Pass score while
   `status` stays honestly `"draft"`) -- never a different rubric's
   weights, never an ad hoc score.
5. **Stop condition.** Once `overall_score >= 80` (`policy-rubric.md`'s
   own `pass_threshold`), report the Policy as passing and stop iterating
   the field loop, even if a lower-weight P-00x criterion is still
   Partial or Fail. A Policy that passes hands off to the Standard loop; a
   Policy with zero Standards cannot itself be proposed later, so the
   session is not treated as "done" until at least one Standard exists.

### Standard authoring loop

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

### Control authoring loop

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

### Web research and citations

Applies immediately before drafting three specific fields -- S-001's
`procedure`, C-002's `execution_method`, and S-004's `verification_notes`
-- each genuinely "how" content, unlike every other criterion's field.

1. **Offer, never require.** Before drafting the field, offer the user a
   web-research lookup using the host's own web-search/browsing
   capability -- **never a PS Service tool call** (no MCP tool call is
   made for the offer itself). If the user declines, or the offer isn't
   taken up, proceed on the user's own drafting as normal -- identical to
   the plain field-by-field flow above, with nothing appended to the
   value.
2. **If research is used, cite it -- honestly.** When the user accepts
   and web research is actually incorporated into the drafted value,
   append a literal `Sources: <url>[, <url>...]` line to the end of that
   field's value, separated from the drafted text by a blank line, before
   calling the same `update-standard-draft`/`update-control-draft` used
   in the field-by-field loop above. List **only the URL(s) actually
   retrieved by that lookup** -- never fabricate a URL, and never
   construct or guess one from memory or plausibility. If research was
   declined or not used, the `Sources:` line is never appended -- the
   suffix is only ever present when it names real, retrieved sources.
3. Neither `update-standard-draft` nor `update-control-draft` requires or
   rejects a `Sources:` suffix -- the tool round-trips the field's value
   either way. The discipline above (append only when research was used,
   only real retrieved URLs, never fabricated, never required) is this
   skill's own, not the tool's.

### Session end

Reached when the user says they are done for now, or every open Standard/
Control cardinality question in this session has been answered "finish".

1. **Holistic rescore.** Re-read every Policy/Standard/Control created or
   touched this session via `cypher` (the same D-5-shaped content read each
   authoring loop above already used per field) and recompute each node's
   own `overall_score` against its own rubric file -- `policy-rubric.md`
   for the Policy, `standard-rubric.md` for each Standard,
   `control-rubric.md` for each Control. Never a single blended score
   across types, and never a different rubric's weights for a given node
   type -- exactly the same discipline the field loops already apply per
   field, re-applied once, holistically, at session end.
2. **Summary table.** Render one row per Policy/Standard/Control created or
   touched this session: its id, its current `overall_score`, pass/fail
   against its own `pass_threshold`, and a one-line cascade-coherence note
   (e.g. "Policy passes; 1 of 2 Standards passes; 1 Control still below
   threshold"). See Output below for the exact shape.
3. **State readiness plainly -- and stop there.** Tell the user this tree
   is ready for their own separate, later request to trigger
   `propose-policy` on the Policy. **Never call `propose-policy`,
   `approve-policy`, `reject-policy`, or `revert-policy-to-draft` here, or
   anywhere else in this skill's own flow** -- every status transition is
   always the user's own separate request, made outside this skill (see
   Guardrails). A Policy with zero Standards cannot itself be proposed
   later (`PolicyIncompleteForProposalError`, a tool this skill never
   calls) -- if the session ends with zero Standards on the Policy, say so
   plainly in the summary rather than implying the tree is propose-ready.

## Guardrails

- The skill reaches PS Service exclusively through a recognised MCP
  connector -- `ps-mcp` --
  never a direct graph connection, a repo-local script, or a spawned
  external binary.
- Never call `propose-policy`, `approve-policy`, `reject-policy`, or
  `revert-policy-to-draft` -- this skill only ever authors content; every
  governance status transition is always the user's own separate, later
  request (see Core Principles and the session-end step of Process above).
- If two or more named Capabilities' `GOVERNED_BY` lookups disagree (e.g.
  one Capability has no governing Policy while another is already governed
  by a Draft or a Proposed/Approved Policy), report the disagreement
  plainly to the user and ask how to proceed (which Capability's situation
  to follow, or whether to split the request into separate sessions) --
  never guess or silently pick one Capability's branch. No
  `create-policy-draft`, `get-policy`, or any other content-CRUD tool call
  is made until the user resolves the disagreement. (Stated under Core
  Principles too; repeated here because it is itself a distinct
  failure-to-proceed state, alongside `governed_by_unowned_draft` in the
  table below, not merely a documentation note.)
- Never collapse the named error states below into each other or into a
  generic message.
- Never fabricate a rubric score, a tool result, a persisted field value,
  or a citation URL -- report exactly what each tool call (or web-research
  lookup) actually returned.
- The title derived at Policy scaffold time can never be corrected
  afterwards through this tool surface (`update-policy-draft` never
  accepts `title`, and no other tool renames an existing Policy) -- if the
  user wants a materially different title later, the only path is
  abandoning this draft and starting a new `create-policy-draft` call.
  This is a known limitation this skill does not attempt to solve, only
  states plainly when relevant.

Report every non-success outcome below as its own named state -- never
collapsed into a generic "action failed". Sourced from
`ps_service.policy_lifecycle.errors` (exact message text), `mcp_server.py`'s
`_parse_patch_fields`/`add-control-to-draft`'s own `control_type` check, and
the `cypher` tool's own docstring -- every message below was verified
against that real source, not copied from paraphrase:

| Tool result shape                                                                                                                                                                                 | Named state to report                                                                                                                                                                                                        | Applies to                                                                                                                                                                  |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Connection/transport failure, or `error: this action requires a real authenticated caller (the local-test bypass counts as one)`                                                                  | `unauthenticated` -- the caller has no real authenticated session (the local-test bypass DOES count as one here)                                                                                                             | `create-policy-draft`, `update-policy-draft`, `add-standard-to-draft`, `update-standard-draft`, `add-control-to-draft`, `update-control-draft`, `get-policy`                |
| `error: the policy graph database is not reachable`                                                                                                                                               | `policy_graph_unavailable` -- the FalkorDB-backed policy graph cannot be reached                                                                                                                                             | `create-policy-draft`, `update-policy-draft`, `add-standard-to-draft`, `update-standard-draft`, `add-control-to-draft`, `update-control-draft`, `get-policy`, plus `cypher` |
| `error: no Policy exists with id '<policy_id>'`                                                                                                                                                   | `policy_not_found` -- no Policy exists with the given id                                                                                                                                                                     | `get-policy`, `update-policy-draft`, `add-standard-to-draft`                                                                                                                |
| `error: no Standard exists with id '<standard_id>'`                                                                                                                                               | `standard_not_found` -- no Standard exists with the given id                                                                                                                                                                 | `update-standard-draft`, `add-control-to-draft`                                                                                                                             |
| `error: no Control exists with id '<control_id>'`                                                                                                                                                 | `control_not_found` -- no Control exists with the given id                                                                                                                                                                   | `update-control-draft`                                                                                                                                                      |
| `error: you do not have access to this Policy`                                                                                                                                                    | `draft_access_denied` -- the caller is neither the owner nor `SystemOwner`/`SystemAdmin`, for a draft governed by someone else; never reveals whether the Policy exists, who owns it, or its status                          | `get-policy`, `update-policy-draft`, `add-standard-to-draft`, `update-standard-draft`, `add-control-to-draft`, `update-control-draft`                                       |
| `error: a Policy titled '<title>' already exists (id '<existing_policy_id>'); amend it via the supersede workflow, or choose a different title`                                                   | `title_already_exists` -- the derived id for the scaffolded title collides with an existing Policy                                                                                                                           | `create-policy-draft`                                                                                                                                                       |
| `error: standards[<i>]...` (or `standards[<i>].controls[<j>]...`)                                                                                                                                 | `malformed_standards_input` -- only reachable if this skill's own D-4 discipline (never using `create-policy-draft`'s optional bulk `standards` argument) is violated; kept for completeness, should never occur in practice | `create-policy-draft`                                                                                                                                                       |
| `error: fields.<key> is not a patchable field` / `error: fields.<key> must be a string or null` / `error: fields.<key> must be one of (...) or null`                                              | `malformed_fields_input` -- the `fields` payload named an unknown key, a non-string/non-null value, or an invalid enum value for `implementation_status`/`type`                                                              | `update-policy-draft`, `add-standard-to-draft`, `update-standard-draft`, `add-control-to-draft`, `update-control-draft`                                                     |
| `error: control_type must be 'automated' or 'manual'`                                                                                                                                             | `invalid_control_type` -- the top-level `control_type` creation-time argument was neither                                                                                                                                    | `add-control-to-draft`                                                                                                                                                      |
| `error: cannot <action> a Policy in status '<current_status>' (requires status '<required_status>')`                                                                                              | `invalid_status_transition` -- the target Policy/Standard/Control's own governance status does not permit this content edit right now (e.g. no longer `"draft"`)                                                             | `update-policy-draft`, `add-standard-to-draft`, `update-standard-draft`, `add-control-to-draft`, `update-control-draft`                                                     |
| `error: Policy '<policy_id>' cannot be superseded: current status is '<actual_status>', requires 'approved'`                                                                                      | `supersede_prior_not_approved` -- the fork branch's governing Policy was only `"proposed"`, not yet `"approved"` (Branch detection)                                                                                          | `create-policy-draft` (fork path)                                                                                                                                           |
| Query contains a write clause (`CREATE`/`MERGE`/`DELETE`/`SET`/`REMOVE`/`DROP`/`FOREACH`)                                                                                                         | `query_rejected_write_clause` -- `cypher` is read-only; rejected before execution                                                                                                                                            | `cypher`                                                                                                                                                                    |
| The graph has no seeded content at all yet (distinct from a query that legitimately matches nothing)                                                                                              | `graph_unseeded`                                                                                                                                                                                                             | `cypher`                                                                                                                                                                    |
| `error: no Capability exists with id(s) '<capability_id>'; check the Capability ids and retry`                                                                                                    | `capability_not_found` -- a `capability_ids` entry matches no Capability; re-run the step 2 existence check, never retry blindly                                                                                             | `create-policy-draft` (fresh path)                                                                                                                                          |
| `error: Capability id(s) '<capability_id>' already governed by a Policy; amend that Policy via the supersede workflow instead`                                                                    | `capability_already_governed` -- the Capability gained a governing Policy since step 3 (or concurrently); re-run branch detection from step 3                                                                                | `create-policy-draft` (fresh path)                                                                                                                                          |
| `error: an unexpected error occurred`                                                                                                                                                             | `unexpected_error` -- an unrecognised failure, distinct from every named state above; never guess at its cause                                                                                                               | every tool this skill calls                                                                                                                                                 |
| (Not a tool error -- a branch-detection-only state, see Core Principles/Process) `get-policy` denied on a Draft whose `GOVERNED_BY` status the branch-detection query already showed as `"draft"` | `governed_by_unowned_draft` -- the Draft belongs to someone else and is not forkable (only an `"approved"` prior can be forked); block and report, never create a second, competing Policy for the same Capability           | branch detection only                                                                                                                                                       |
| (Not a tool error -- a branch-detection-only state) `get-policy` denied on the draft `fork_id` of a `SUPERSEDED_BY` successor of the governing Policy                                             | `superseded_by_unowned_draft` -- the fork belongs to someone else; block and report, never create a second fork                                                                                                              | branch detection only                                                                                                                                                       |
| (Not a tool error -- a branch-detection-only state) the governing Policy's `SUPERSEDED_BY` successor has `fork_status == "proposed"`                                                              | `fork_awaiting_approval` -- the fork awaits the user's own separate approval; block and report, never create a second fork                                                                                                   | branch detection only                                                                                                                                                       |
| Two or more named Capabilities' `GOVERNED_BY` results disagree                                                                                                                                    | `capability_governance_disagreement` -- report and ask the user how to proceed, per the bullet above                                                                                                                         | branch detection only                                                                                                                                                       |
| Successful structured response                                                                                                                                                                    | -- report the scaffold, the persisted field, the tree, or the session summary, whichever this step of Process produced                                                                                                       | every tool this skill calls                                                                                                                                                 |

## Output

In this shape on success, one fenced block per outcome shape:

A fresh scaffold (Policy authoring loop step 1, or the analogous
add-Standard/add-Control scaffold):

```text
Scaffolded draft Policy: <policy_id> -- "<title>"
  Status: draft

  scope_in:  <provisional scope_in text>
  scope_out: <provisional scope_out text>
```

A per-field confirmation, after each Socratic answer is persisted (Policy/
Standard/Control field loops, step "Immediate-persist-then-rescore"):

```text
Persisted <property>: "<value>"
  <Policy|Standard|Control> <id> -- overall_score now <n> / 100 (pass_threshold <t>)
```

A cardinality question, once a Standard's or a Control's own rubric passes
(or, for Control, the user declines to add one):

```text
<Standard|Control> <id> passes (overall_score <n> >= <t>).
Add another <Standard|Control>, <move to Controls | add another Standard>, or finish?
```

The session-end summary table (Session end step 2):

```text
Session summary:
  Policy <policy_id> -- "<title>": <n>/100 (<pass|fail>, pass_threshold <t>)
    Standard <standard_id> -- "<title>": <n>/100 (<pass|fail>)
      Control <control_id> -- "<title>": <n>/100 (<pass|fail>)
      ...
    ...

This tree is ready for your own separate request to trigger
`propose-policy` on <policy_id> -- this skill will not call it for you.
```

A named error state (Guardrails table above):

```text
<named_state>: <the tool's own error text>
```

On a named error state, report that state plainly instead of any of the
blocks above -- never emit an Output block that implies a successful
scaffold, persisted field, or session summary when none occurred.
