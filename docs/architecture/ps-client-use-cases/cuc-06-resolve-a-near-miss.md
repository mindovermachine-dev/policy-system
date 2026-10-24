<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-06: Resolve a near-miss

**Status:** Specified
**Actor:** An authenticated reviewer for keep-separate; a Compliance Officer (explicit
`ComplianceOfficer` grant) for merge
**Goal:** A Company Merge dedup candidate (`PendingReview`, of a node kind that has a
review spec; today Capability and Role) resolved by a human decision — the pair kept separate, or
merged with the effect previewed, signed, audited, and reversible.
**Realizes:** UC-1
**Interaction class:** Review/resolve
**v1:** yes

## Entry points

- An Inbox item: near-miss reviews are the canonical system-generated assignment —
  Company Merge mints them (CUC-04's consequence path), the Inbox surfaces them. Who they
  are assigned to is Q-12.
  Company Merge raises a review only for a pair scoring at or above the review threshold,
  so the queue holds candidates worth a human look rather than every close-ish pair.
- A new conversation: "any near-misses to review?"
- The Library (CUC-11): resuming a review session, including one left mid-approval.

## Preconditions

- Caller is authenticated. Listing is read-only; resolving writes. Merging a Capability
  or Role pair needs the explicit `ComplianceOfficer` grant (SystemOwner and SystemAdmin do not
  imply it, and the local-test bypass does not satisfy it); keep-separate needs only
  authentication.
- At least one unresolved review exists (an empty list is stated plainly, never implied).

## Beats

1. **Surface.** The unresolved reviews render as a read-only report: every review with
   all five of its fields — `id`, `kind`, incoming text, nearest-existing text,
   `similarity` — never summarized down; a reviewer needs all five to judge a pair. The
   `kind` names the node kind under review. A stored review whose kind has no registered
   spec is refused, naming that kind (HTTP 409 `review_kind_unsupported`), rather than
   skipped, so it can be found and cleared.
2. **Open one.** Selecting a review renders a **comparison artifact**: the two entities
   side by side, and the effect of each decision previewed explicitly before anything is
   asked — keep-separate deletes only the review record and touches neither entity;
   merge is available for kinds that have a merge executor (Capability and Role), the CUC-07
   Capability merge: the loser is deleted behind a `MergedCapability`
   marker naming the winner, its `REQUIRES`, `COVERS` and `MITIGATED_BY` edges
   move to the winner, and the review record is deleted in the same write. The preview
   names the winner (the Capability with the earlier `created_at`; a tie goes to the
   incoming node), the edge counts and the Policy case. A **Role** review is two Roles of
   one instrument and merges through the Role merge (`merge-roles`, same `ComplianceOfficer`
   gate and passkey approval): the absorbed Role is deleted behind a `MergedRole` marker, its
   Obligations move to the survivor, and `unmerge-entities` reverses it. A Role has no governing
   Policy, so no governance acknowledgment applies. The controls live on the
   artifact; chat advises, the click is the user's.
3. **Keep separate.** No ceremony: the decision executes at once, is audited at the
   request, and the artifact shows the review closed with both entities untouched.
4. **Merge.** The merge runs the full danger-zone shape: the decision returns the cleanup
   merge preview and creates a one-time `merge-capabilities` approval — bound to exactly
   this review, this pair, this state and the Policy acknowledgment, with an expiry — and
   the passkey ceremony runs against PS Service's own WebAuthn relying party. No graph
   write happens before the signature; the merge executes when the approver signs, with
   the signer as the audited actor, who must still hold `ComplianceOfficer`. A
   Capability keeps one governing Policy: when exactly one of the pair is governed the
   officer must acknowledge the governance change before an approval exists, and when the
   two are governed by different Policies the merge is refused with a pointer to
   `release-capability-governance` (keep-separate stays available). The write is audited
   as a `capability.merge` row with before/after snapshots, linked to the review id.
   Declining or letting the approval expire changes nothing, and the artifact says so.
5. **Outcome.** The artifact updates to the resolved state: winner and loser ids on a
   merge, both-untouched on keep-separate. Every failure keeps its name, visibly:
   not-found/already-resolved (indistinguishable server-side, by design), **stale** (the
   review's entity was already consumed by a different merge — no write), audit trail
   unavailable (the merge did not run *and* the one-time approval is consumed — a fresh
   approval must be requested), unreachable service or graph. Nothing is silently
   retried.
6. **The queue.** The Inbox count reflects the resolution; the next unresolved review is
   one step away. One merge can stale other reviews on the same entity — the list shows
   current state, not the state at first render.

## Artifacts

- The unresolved-reviews listing: a read-only report.
- One **comparison artifact** per opened review: the pair, the previewed effects, the
  decision controls, then the resolved record. Non-editable, non-scored.

## Approvals & notifications

- **Passkey ceremony on merge only** — the review/resolve instance of the lifecycle-only
  ceremony rule; the signing summary binds to the exact pair and review state.
  Keep-separate never triggers one.
- **Reversible.** `unmerge-entities` (CUC-07) recreates the loser from the `capability.merge`
  snapshot and removes its marker. It does not recreate the deleted review: the pair is not offered for review
  again unless a later ingest or restore detects it anew.
- Resolving clears the review's Inbox item. No new notifications are emitted.

## Contracts touched

- **H-1 Tool loop** — listing, resolution, and approval-status checks as client tool
  calls; a pending approval is resumable from a later conversation.
- **H-4 Permissions & approvals** — the effect-preview-then-acknowledge surface; the
  near-miss elicitation is the established precedent this contract generalizes.
- **PSC-6 Conversation persistence** — the review session and its comparison artifacts
  persist and resume, mid-approval included.
- **PSC-7 Notifications & Inbox** — near-miss reviews are the system-generated Inbox
  source; Q-12's assignment model decides their routing.
- **PSC-9 Auth flows** — the signing ceremony embedded in the client; today it is an
  `approval_url` opened in a companion browser, the exact gap SP-4 tests.
- **PSC-10 Artifact type registry** — the comparison type's flags drive the pane.

## Open points

- **Gate decided**: a near-miss merge needs the explicit `ComplianceOfficer` grant to
  create and to execute, like graph cleanup's sibling merges. Keep-separate stays open to
  any authenticated caller. Whether Q-12 assignment also narrows who sees a review is
  still open.
- Staleness in an open comparison artifact: a review can go stale while on screen
  (another merge consumes its entity). The signing binding protects the write; whether
  the pane re-validates before offering the ceremony, or lets the named stale error
  carry it, is NFR-3 presentation left open.
- Whether resolving one review auto-advances to the next (queue flow) or returns to the
  list — presentation, but it shapes the Inbox experience.
