---
name: ps-near-miss-review
description: List and resolve near-miss pending reviews (Company Merge dedup candidates) in the Policy System compliance graph — keep two entities separate or merge them, with the merge effect made explicit before it runs.
---

# ps-near-miss-review

## Purpose

List every unresolved near-miss `PendingReview` — a Company Merge dedup
candidate awaiting a human decision — keep a pair separate, or merge a set of
chosen reviews under one passkey approval. This replaces ps-cli's `near-misses list`/
`near-misses resolve` commands (issue #126) with MCP-tool-backed equivalents
reachable by any MCP-capable client, with no locally-installed, signed
ps-cli binary required.

**Scope covered so far:** listing unresolved reviews (`list-near-misses`),
resolving one with `decision="keep-separate"` (`resolve-near-miss`), and merging
a set of chosen reviews (`merge-near-misses`), then following the batch's passkey
approval and the outcome of each merge (`check-near-miss-merges`). A merge is the
Graph Cleanup Capability or Role merge: the tool returns one listed preview and
one approval link for the whole set, and a Compliance Officer signs it once with
a passkey. This skill's own job is to make sure the user has seen and understood
what the merge does, that they chose exactly which reviews to merge, and that it
needs the `ComplianceOfficer` role, before the approval is requested.

A review's `kind` names the node kind under review. Only kinds that have a review
spec appear (today Capability and Role; a Role review compares two Roles of one
instrument), a review is raised only for a pair scoring at or above the configured
review threshold of its kind, and a merge is available for kinds that have a merge
executor (today Capability and Role).

**Deliverable:** every unresolved review's `id`, `kind`, `incoming_text`,
`nearest_existing_text`, and `similarity` on listing; for `keep-separate` the
resolved review's `review_id` and `decision`; for a merge the listed preview
(one entry per chosen review: survivor and absorbed node, Policy case and edges
that move for a Capability, `state_digest`) and the approval link, then each
merge's `state`, `winner_id` and `loser_id` once the approval is signed — or the
specific named error state on failure.

## On Load

Exactly one connector name is recognised: `ps-mcp` ("Policy System MCP"),
the connector the Policy System Plugin declares, pointing at a hosted PS
Service. Claude lists it as `plugin:ps-plugin:ps-mcp`. Any other name is not
a PS Service connector. A connector that is present
under the right name but does not expose the `list-near-misses`,
`resolve-near-miss`, `merge-near-misses` and `check-near-miss-merges` tools is **not** a PS Service connector either,
whatever it is named — report it as unreachable (see the error-state
table under Process) rather than proceeding against it. From here on,
"the PS Service connector" means the `ps-mcp` connector.

**Audit:** a `keep-separate` decision is recorded as a `near_miss.resolve` row
(both entity ids and the decision) at the request. Each merge of a batch is recorded as its own
`capability.merge` row (`role.merge` for a Role review), written when the approver signs with their passkey and
before that merge changes the graph. It names the approver, the survivor, the absorbed
node, the review id and a before/after snapshot of the graph. A merge that fails
writes a `failed` row for its own absorbed node and the same approval; the other merges of the
batch are not affected. If the audit trail is unavailable at that moment that merge is not
performed and the one-time approval is already consumed, so a new approval must be requested.
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
- `merge-near-misses` needs an explicit `ComplianceOfficer` grant. SystemOwner
  and SystemAdmin do not stand in for it, and neither does the local-test
  bypass; a caller without it gets an `error:` and no approval exists. Tell
  the user this before proposing a merge, and do not offer the merge to a
  user who has said they are not a Compliance Officer; `keep-separate` needs
  no such role.
- `merge-near-misses` does not execute anything by itself: it returns a
  listed preview and one pending passkey approval (`pending_approval_id`,
  `approval_url`, `expires_at`) that binds every chosen merge. Only when a
  Compliance Officer opens the link and signs with a passkey do the merges run.
  Never present a merge as done before `check-near-miss-merges` reports it
  `merged`.
- The user chooses the set. Never pick, add, drop or reorder the reviews to
  merge, and never merge "all" reviews on the user's behalf; the set is the
  review ids the user named, in the order they named them. There is no limit on
  its size.
- State what a merge does, in these terms, before proposing it: the
  Capability with the earlier `created_at` survives (a missing one counts
  earliest, a tie goes to the incoming node). The other is deleted and a
  `MergedCapability` marker naming the survivor remains, so a later
  ingest or restore resolves to the survivor and raises no new near miss. Its
  `REQUIRES`, `COVERS` and `MITIGATED_BY` edges move to the survivor, and the
  review record is deleted. For a `Role` review the merge is the Role merge
  instead: the incoming Role survives, the absorbed Role's Obligations move to the
  survivor, the absorbed Role is deleted behind a `MergedRole` marker, and no Policy is
  involved. Each merge is one all-or-nothing write; the merges of a batch run one after
  another, so one can fail while the others succeed. A merge can be
  reversed afterwards with the `unmerge-entities` tool (see `ps-graph-cleanup`), which
  does not restore the review record.
- A Capability has at most one governing Policy. When exactly one of the two
  is governed the preview shows `policy_case` 2 and the survivor ends up
  governed by that Policy, which changes what it governs; the tool returns
  `acknowledgment_required` and creates no approval until the officer has
  accepted that change for that review and the call is repeated with its id in
  `acknowledged_review_ids`. Never acknowledge a review the officer has not
  accepted by name. When the two are governed by different Policies
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

### Merging chosen reviews under one approval

1. **State the effect first**, in plain language, before asking which reviews to
   merge: the deleted node and its redirect marker, the edges or Obligations that
   move, the one-Policy rule, that the merges run one after another so one can fail
   while the others succeed, the single passkey approval and the `ComplianceOfficer`
   requirement (see Core Principles). Do this even if the user already said "merge" —
   the point is that they have seen the effect stated, not merely typed the word.
2. **Let the user name the reviews.** List first (see Listing reviews) if the user
   has not named any, then ask which reviews to merge. Only the user names which reviews to
   merge, by `id`; never choose, add or drop a review yourself, and read the set back
   for confirmation before calling anything.
3. **Call the tool.** Invoke `merge-near-misses` with `review_ids` set to the confirmed
   ids in the order named. Leave `acknowledged_review_ids` unset on the first call.
4. **Handle a required acknowledgment.** If the response is
   `{"acknowledgment_required": true, "reviews", "message", "merges"}`, show for each
   listed review the affected Policy (`policy.id`, `policy.title`, `policy.status`) and
   the `message`, and ask about one review at a time whether the officer accepts the
   governance change. Do not acknowledge all at once (never acknowledge all), and never acknowledge a review the
   officer has not accepted by name. A Role review takes no acknowledgment. Repeat the
   call with only the accepted ids in `acknowledged_review_ids`; a review left out stays
   unacknowledged and the tool asks again.
5. **Show the listed preview.** A successful response is
   `{"merges", "pending_approval_id", "approval_url", "expires_at"}` with one entry per
   review. For each, report the `review_id`, `kind`, the survivor and the absorbed node
   (ids and names), whether it was `acknowledged`, the `state_digest`, and for a
   Capability the `policy_case` and the edges that move (`edges_to_move`), and for a
   Role the moved Obligations (`moved_obligation_ids`). Then give the `approval_url` with
   its `expires_at`, tell the user to open the link in a browser and sign once with a
   passkey, and state explicitly that nothing has changed yet.
6. **Follow the approval.** When the user says they have signed, call
   `check-near-miss-merges` with the `pending_approval_id` and report `status`
   (`pending`, `expired` or `signed`). Once `signed`, report each merge by its
   `review_id` and `state`: `merged` with its `winner_id` and `loser_id`; `failed` with
   its `error`, which means that merge was not carried out and nothing about it changed;
   or `unsettled`, which means the outcome of that merge is not yet known — say so, tell
   the user to check again later, and never report an `unsettled` merge as done or as
   failed. Never retry silently; a new `merge-near-misses` call creates a new approval.
7. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "merge failed":

   | Tool result shape                                                                                                                 | Named state to report                                                                                                                                                                                                                                    |
   | --------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error                                                                   | "PS Service is unreachable or the caller is unauthenticated"                                                                                                                                                                                             |
   | `error: graph cleanup requires a real authenticated caller` or `error: You do not have the required access role for this action.` | "Merge needs a Compliance Officer" — the caller has no real session or no explicit `ComplianceOfficer` grant; no approval was created and nothing changed                                                                                                |
   | An `error:` naming a review that does not exist, is already resolved, or is stale                                                 | "Review not found, already resolved or stale" — the error names every such review with its state; no approval was created. A stale review is distinct from a missing one: the review exists but a node it compared was already consumed by another merge |
   | An `error:` naming reviews that share a node                                                                                      | "Reviews overlap" — two chosen reviews touch the same node; no approval was created, the user must choose a set without the overlap                                                                                                                      |
   | An `error:` naming `release-capability-governance`                                                                                | "A Capability pair is governed by two different Policies" — no approval was created; the officer must release one Policy's governance first, or keep the pair separate                                                                                   |
   | An `error:` about an acknowledged review id that is not selected, is a Role review or changes no governance                       | "Invalid acknowledgment" — report which review; no approval was created                                                                                                                                                                                  |
   | `{"acknowledgment_required": true, ...}`                                                                                          | "Acknowledgment needed" — not an error; show each Policy and the message and wait for the officer, as in step 4                                                                                                                                          |
   | `error: no reviewable kind is registered for '<kind>'`                                                                            | "Review of an unsupported kind" — a stored review has a kind that has no review spec, so the list or the decision is refused (HTTP 409 `review_kind_unsupported`); name the kind from the message, change nothing, and do not guess at a workaround      |
   | `error: the policy graph database is not reachable`                                                                               | The compliance graph database cannot be reached — report it distinctly from a PS Service transport failure                                                                                                                                               |
   | `error: no pending approval with id ...` from `check-near-miss-merges`                                                            | "Approval not found" — the id is wrong or the approval is not the caller's own batch; the two are indistinguishable by design                                                                                                                            |
   | `error: an unexpected error occurred`                                                                                             | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                                                                                                                        |
   | Successful structured response                                                                                                    | Report the listed preview and the approval link as in step 5 and state explicitly that nothing has changed yet; the merges happen only when the approval is signed                                                                                       |

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
- Merges change the graph only when a Compliance Officer signs the one passkey
  approval. Never describe a merge as done before `check-near-miss-merges`
  reports it `merged`, never report an `unsettled` merge as done or as failed,
  never acknowledge a review the officer has not accepted by name, never choose the set of
  reviews for the user, and never offer the merge to a caller who lacks the
  `ComplianceOfficer` role.
