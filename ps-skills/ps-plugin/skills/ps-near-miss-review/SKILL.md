---
name: ps-near-miss-review
description: List and resolve near-miss pending reviews (Company Merge dedup candidates) in the Policy System compliance graph — keep two entities separate or merge them, with the merge effect made explicit before it runs.
---

# ps-near-miss-review

## Purpose

List every unresolved near-miss `PendingReview` — a Company Merge dedup
candidate awaiting a human decision — and resolve one by keeping its two
entities separate or merging them. This replaces ps-cli's `near-misses list`/
`near-misses resolve` commands (issue #126) with MCP-tool-backed equivalents
reachable by any MCP-capable client, with no locally-installed, signed
ps-cli binary required.

**Scope covered so far:** listing unresolved reviews (`list-near-misses`),
resolving one with `decision="keep-separate"`, and resolving one with
`decision="merge"` (`resolve-near-miss`), then following the merge's passkey
approval (`check-near-miss-approval`). A merge is the Graph Cleanup Capability
merge: the tool returns a preview and an approval link, and a Compliance
Officer signs it with a passkey. This skill's own job is to make sure the user
has seen and understood what the merge does, and that it needs the
`ComplianceOfficer` role, before the approval is requested.

A review's `kind` names the node kind under review. Only kinds that have a review
spec appear (today Capability and Role; a Role review compares two Roles of one
instrument), a review is raised only for a pair scoring at or above the configured
review threshold of its kind, and a merge is available for kinds that have a merge
executor (today Capability and Role).

**Deliverable:** every unresolved review's `id`, `kind`, `incoming_text`,
`nearest_existing_text`, and `similarity` on listing; for `keep-separate` the
resolved review's `review_id` and `decision`; for `merge` the preview (survivor
and absorbed Capability, Policy case, edges that move, `state_digest`) and the
approval link, then the `winner_id` and `loser_id` once the approval is signed
— or the specific named error state on failure.

## On Load

Exactly one connector name is recognised: `ps-mcp` ("Policy System MCP"),
the connector the Policy System Plugin declares, pointing at a hosted PS
Service. Claude lists it as `plugin:ps-plugin:ps-mcp`. Any other name is not
a PS Service connector. A connector that is present
under the right name but does not expose both a `list-near-misses` tool
and a `resolve-near-miss` tool is **not** a PS Service connector either,
whatever it is named — report it as unreachable (see the error-state
table under Process) rather than proceeding against it. From here on,
"the PS Service connector" means the `ps-mcp` connector.

**Audit:** a `keep-separate` decision is recorded as a `near_miss.resolve` row
(both entity ids and the decision) at the request. A `merge` is recorded as a
`capability.merge` row (`role.merge` for a Role review), written when the approver signs with their passkey and
before the graph is changed. It names the approver, the survivor, the absorbed
Capability, the review id and a before/after snapshot of the graph. A failed
or refused signing writes a `failed` row for the same approval. If the audit
trail is unavailable at that moment the merge is not performed and the
one-time approval is already consumed, so a new approval must be requested.
Read the rows with `ps-list-audit-events`.

## Core Principles

- Never fabricate a review or a field value — report exactly what the tool
  returned, for every unresolved review, not just a subset.
- Never collapse the five reported fields (`id`, `kind`, `incoming_text`,
  `nearest_existing_text`, `similarity`) into a shortened summary — a
  reviewer needs all five to judge the pair.
- Never silently retry a failed call — report the named failure and stop.
- Never resolve a review without the user having named which review
  (`id`) and which decision they want — never guess or default either.
- `decision="merge"` needs an explicit `ComplianceOfficer` grant. SystemOwner
  and SystemAdmin do not stand in for it, and neither does the local-test
  bypass; a caller without it gets an `error:` and no approval exists. Tell
  the user this before proposing the merge, and do not offer the merge to a
  user who has said they are not a Compliance Officer; `keep-separate` needs
  no such role.
- `decision="merge"` does not execute anything by itself: it returns a
  preview and a pending passkey approval (`pending_approval_id`,
  `approval_url`, `expires_at`). Only when a Compliance Officer opens the link
  and signs with a passkey does the merge run. Never present the merge as
  done before `check-near-miss-approval` reports it signed.
- State what the merge does, in these terms, before proposing it: the
  Capability with the earlier `created_at` survives (a missing one counts
  earliest, a tie goes to the incoming node). The other is deleted and a
  `MergedCapability` marker naming the survivor remains, so a later
  ingest or restore resolves to the survivor and raises no new near miss. Its
  `REQUIRES`, `COVERS` and `MITIGATED_BY` edges move to the survivor, and the
  review record is deleted. For a `Role` review the merge is the Role merge
  instead: the absorbed Role's Obligations move to the survivor, the absorbed
  Role is deleted behind a `MergedRole` marker, no Policy is involved (never
  set `acknowledge_governance_change`), and `unmerge-entities` reverses it. The merge is one all-or-nothing write and can be
  reversed afterwards with the `unmerge-entities` tool (see `ps-graph-cleanup`), which
  does not restore the review record.
- A Capability has at most one governing Policy. When exactly one of the two
  is governed the preview shows `policy_case` 2 and the survivor ends up
  governed by that Policy, which changes what it governs; the tool returns
  `acknowledgment_required` and creates no approval until the officer has
  accepted that change and the call is repeated with
  `acknowledge_governance_change=true`. Never set the flag without the
  officer's explicit say-so. When the two are governed by different Policies
  the merge is refused with an error that names `release-capability-governance`;
  explain that one Policy's governance must be released first (see
  `ps-graph-cleanup`), and that `keep-separate` stays available.
- `decision="keep-separate"` only deletes the pending review record
  itself — it never touches the two entities the review compared, and it
  needs no approval and no `ComplianceOfficer` role.

## Process

### Listing reviews

1. **Call the tool.** Invoke `list-near-misses` (no arguments) on the PS
   Service connector selected at On Load. This is a read-only call.
2. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "listing failed":

   | Tool result shape                                                                     | Named state to report                                                                                                                                                                                                                               |
   | ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error                       | "PS Service is unreachable or the caller is unauthenticated"                                                                                                                                                                                        |
   | `error: no reviewable kind is registered for '<kind>'`                                | "Review of an unsupported kind" — a stored review has a kind that has no review spec, so the list or the decision is refused (HTTP 409 `review_kind_unsupported`); name the kind from the message, change nothing, and do not guess at a workaround |
   | `error: the policy graph database is not reachable`                                   | The compliance graph database cannot be reached — report it distinctly from a PS Service transport failure                                                                                                                                          |
   | `error: The audit trail is temporarily unavailable; the operation was not performed.` | The audit trail could not record the operation, so it was NOT run (nothing was changed) — report it distinctly from an outage of the graph or of PS Service                                                                                         |
   | `error: an unexpected error occurred`                                                 | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                                                                                                                   |
   | Successful structured response                                                        | Report every unresolved review's `id`, `kind`, `incoming_text`, `nearest_existing_text`, and `similarity` plainly, including an explicit "no unresolved reviews" statement when the list is empty                                                   |

3. **Output**, in this shape on success:

   ```text
   Unresolved near-misses: <count>

     <id> [<kind>] similarity=<similarity>
       incoming: <incoming_text>
       existing: <nearest_existing_text>
     ...
   ```

   On a named error state, report that state plainly instead — do not emit
   an Output block that implies a successful listing when none occurred.

### Resolving a review with `keep-separate`

1. **Confirm the inputs with the user** before calling anything: which
   review `id` (normally one already surfaced by a prior listing step —
   list first if the user hasn't named one and doesn't already know it),
   and that they want `decision="keep-separate"`. Never infer the `id` or
   default the decision.
2. **Call the tool.** Invoke `resolve-near-miss` with `review_id` and
   `decision="keep-separate"` on the same PS Service connector.
3. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "resolve failed":

   | Tool result shape                                                                     | Named state to report                                                                                                                                                                                                                               |
   | ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error                       | "PS Service is unreachable or the caller is unauthenticated"                                                                                                                                                                                        |
   | `error: no unresolved PendingReview with id '<review_id>'`                            | "Review not found or already resolved" — the review either never existed or was already resolved (these two conditions are indistinguishable server-side, by design; report them as one state, not a guess at which)                                |
   | `error: no reviewable kind is registered for '<kind>'`                                | "Review of an unsupported kind" — a stored review has a kind that has no review spec, so the list or the decision is refused (HTTP 409 `review_kind_unsupported`); name the kind from the message, change nothing, and do not guess at a workaround |
   | `error: the policy graph database is not reachable`                                   | The compliance graph database cannot be reached — report it distinctly from a PS Service transport failure                                                                                                                                          |
   | `error: The audit trail is temporarily unavailable; the operation was not performed.` | The audit trail could not record the operation, so it was NOT run (nothing was changed) — report it distinctly from an outage of the graph or of PS Service                                                                                         |
   | `error: an unexpected error occurred`                                                 | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                                                                                                                   |
   | Successful structured response                                                        | Report the resolved review's `review_id` and `decision` plainly; state explicitly that `keep-separate` only removed the pending review record and left both compared entities untouched                                                             |

### Resolving a review with `merge`

1. **Explain the effect first, in plain language, before proposing or
   confirming this path with the user:** the deleted Capability and the
   `MergedCapability` redirect marker, the edges that move, the one-Policy rule, the passkey approval
   and the `ComplianceOfficer` requirement (see Core Principles). Do this even
   if the user already said "merge" — the point is that they have seen the
   effect stated, not merely typed the word.
2. **Confirm the inputs with the user** before calling anything: which
   review `id` (normally one already surfaced by a prior listing step —
   list first if the user hasn't named one and doesn't already know it),
   and that they want `decision="merge"` specifically. Never infer the `id`
   or default the decision.
3. **Call the tool.** Invoke `resolve-near-miss` with `review_id` and
   `decision="merge"` on the same PS Service connector. Leave
   `acknowledge_governance_change` unset on the first call.
4. **Show the preview.** A successful response is
   `{"preview", "pending_approval_id", "approval_url", "expires_at"}`. Report
   the survivor and the absorbed Capability (ids and names), the `policy_case`,
   the edges that move (`requires`, `covers`, `mitigated_by`), the
   `state_digest`, and the `approval_url` with its expiry. Tell the user to
   open the link in a browser and sign with a passkey. If the response is
   `{"preview", "acknowledgment_required": true, "message"}` instead, show the
   Policy that is affected (`preview.governance`) and the `message`, ask the
   officer whether they accept the change, and only on an explicit yes repeat
   the call with `acknowledge_governance_change=true`.
5. **Follow the approval.** When the user says they have signed, call
   `check-near-miss-approval` with the `pending_approval_id` and report
   `status` (`pending`, `expired` or `signed`). Once `signed`, `winner_id` and
   `loser_id` name the survivor and the absorbed Capability; a non-null `error` means the
   signature was valid but the merge was not carried out and nothing changed.
   Never retry silently; a new `resolve-near-miss` call creates a new
   approval.
6. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "resolve failed":

   | Tool result shape                                                                                                                           | Named state to report                                                                                                                                                                                                                                 |
   | ------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error                                                                             | "PS Service is unreachable or the caller is unauthenticated"                                                                                                                                                                                          |
   | `error: graph cleanup requires a real authenticated caller` or `error: You do not have the required access role for this action.`           | "Merge needs a Compliance Officer" — the caller has no real session or no explicit `ComplianceOfficer` grant; no approval was created and nothing changed                                                                                             |
   | `error: no unresolved PendingReview with id '<review_id>'`                                                                                  | "Review not found or already resolved" — the review either never existed or was already resolved (these two conditions are indistinguishable server-side, by design; report them as one state, not a guess at which)                                  |
   | `error: pending review '<review_id>' references a node that no longer exists (already resolved by a prior merge); this review is now stale` | "Review references an already-merged node (stale)" — report this distinctly from "not found": the review itself exists, but the entity it compared no longer does, because a different merge already resolved it first; no write happened here either |
   | An `error:` naming `release-capability-governance`                                                                                          | "The two Capabilities are governed by different Policies" — no approval was created; the officer must release one Policy's governance first, or keep the pair separate                                                                                |
   | `{"preview", "acknowledgment_required": true, "message"}`                                                                                   | "Acknowledgment needed" — not an error; show the Policy and the message and wait for the officer, as in step 4                                                                                                                                        |
   | `error: no reviewable kind is registered for '<kind>'`                                                                                      | "Review of an unsupported kind" — a stored review has a kind that has no review spec, so the list or the decision is refused (HTTP 409 `review_kind_unsupported`); name the kind from the message, change nothing, and do not guess at a workaround   |
   | `error: the policy graph database is not reachable`                                                                                         | The compliance graph database cannot be reached — report it distinctly from a PS Service transport failure                                                                                                                                            |
   | `error: an unexpected error occurred`                                                                                                       | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                                                                                                                     |
   | Successful structured response                                                                                                              | Report the preview and the approval link as in step 4 and state explicitly that nothing has changed yet; the merge happens only when the approval is signed                                                                                           |

## Guardrails

- The skill reaches PS Service exclusively through a recognised MCP
  connector — `ps-mcp` — never
  a direct graph connection, a repo-local script, or a spawned external
  binary.
- Never collapse the named error states in the Process tables into each
  other or into a generic failure message.
- Never fabricate a review, a field value, or a count that the tool did not
  actually return.
- Never omit a returned error — surface it plainly rather than treating a
  failed call as an empty list.
- A merge changes the graph only when a Compliance Officer signs its passkey
  approval. Never describe it as done before `check-near-miss-approval`
  reports it signed, never set `acknowledge_governance_change` without the
  officer's explicit acceptance, and never offer the merge to a caller who
  lacks the `ComplianceOfficer` role.
