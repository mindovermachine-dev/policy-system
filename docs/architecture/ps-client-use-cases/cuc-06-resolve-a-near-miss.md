<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-06: Resolve a near-miss

**Status:** Specified
**Actor:** An authenticated reviewer; today the near-miss path carries no AccessRole gate
(see open points)
**Goal:** A Company Merge dedup candidate (`PendingReview`) resolved by a human decision —
the pair kept separate, or merged with the effect previewed, signed, and audited.
**Realizes:** UC-1
**Interaction class:** Review/resolve
**v1:** yes

## Entry points

- An Inbox item: near-miss reviews are the canonical system-generated assignment —
  Company Merge mints them (CUC-04's consequence path), the Inbox surfaces them. Who they
  are assigned to is Q-12.
- A new conversation: "any near-misses to review?"
- The Library (CUC-11): resuming a review session, including one left mid-approval.

## Preconditions

- Caller is authenticated. Listing is read-only; resolving writes.
- At least one unresolved review exists (an empty list is stated plainly, never implied).

## Beats

1. **Surface.** The unresolved reviews render as a read-only report: every review with
   all five of its fields — `id`, `kind`, incoming text, nearest-existing text,
   `similarity` — never summarized down; a reviewer needs all five to judge a pair.
2. **Open one.** Selecting a review renders a **comparison artifact**: the two entities
   side by side, and the effect of each decision previewed explicitly before anything is
   asked — keep-separate deletes only the review record and touches neither entity;
   merge deletes the loser and re-points every edge that referenced it onto the winner,
   atomically and irreversibly. The controls live on the artifact; chat advises, the
   click is the user's.
3. **Keep separate.** No ceremony: the decision executes at once, is audited at the
   request, and the artifact shows the review closed with both entities untouched.
4. **Merge.** The irreversible path runs the full danger-zone shape: the decision creates
   a one-time pending approval — bound to exactly this review, this pair, this state,
   with an expiry — and the passkey ceremony runs against PS Service's own WebAuthn
   relying party. No graph write happens before the signature; the merge executes when
   the approver signs, with the signer as the audited actor. Declining or letting the
   approval expire changes nothing, and the artifact says so.
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

- **No AccessRole gate today**: any authenticated caller can resolve a near-miss, while
  graph cleanup's sibling merges demand an exact `ComplianceOfficer` grant. Whether v1
  gates resolution by role, by Q-12 assignment, or both is undecided — the asymmetry
  looks accidental.
- Staleness in an open comparison artifact: a review can go stale while on screen
  (another merge consumes its entity). The signing binding protects the write; whether
  the pane re-validates before offering the ceremony, or lets the named stale error
  carry it, is NFR-3 presentation left open.
- Whether resolving one review auto-advances to the next (queue flow) or returns to the
  list — presentation, but it shapes the Inbox experience.
