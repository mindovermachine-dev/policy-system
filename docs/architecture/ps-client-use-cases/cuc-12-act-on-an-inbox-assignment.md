<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-12: Act on an Inbox assignment

**Status:** Specified (v1 minimal)
**Actor:** Any authenticated user with pending decisions; the worked flow is a
`PolicyManager` approving or rejecting a proposed Policy tree
**Goal:** Work that needs this user's decision is findable in one place, opens into the
flow that resolves it, and disappears when resolved — by them or by anyone else.
**Realizes:** —
**Interaction class:** —
**v1:** yes (minimal — see the Q-12 constraint below)

**The constraint this is written under:** PS Service has no work-assignment model (Q-12).
Nothing server-side stores an assignment, its state, or its assignee; lifecycle approval
is role-gated, not assignment-gated. The v1-minimal Inbox therefore holds **derived
items** — views computed from existing server state — not work-item records.

## Entry points

- The Inbox itself, in the User Pane — this use case is the third converging door
  (CUC-11), described from its own side.
- A notification may point at an Inbox item, but notifications say "finished" and the
  Inbox says "needs you" — the distinction stays.

## Preconditions

- Caller is authenticated; which items they see follows from what they could act on:
  unresolved near-miss reviews, and — for a `PolicyManager` — proposed Policy trees they
  did not author.

## Beats

1. **The list.** The Inbox shows every derived item with what it is and what decision it
   wants: a near-miss awaiting judgement (CUC-06), a proposed Policy tree awaiting
   approval. Items are views, not records — there is no "mark done," no snooze, no
   assignee; an item exists exactly as long as the underlying state does.
2. **Open an item.** Opening converges into the owning flow with CUC-11's restore
   semantics. A near-miss item lands in CUC-06's comparison artifact. An approval item
   renders the proposed tree read-only — full content, final scores, badges — together
   with the document's continuous thread: the approver reads the governance record that
   produced what they are judging (Q-10's first real instance: a second person inside a
   document's thread).
3. **The approval decision.** The flow only this use case carries. Approve and Reject
   live on the artifact; chat can walk the tree and the scores but never clicks. The
   effect is previewed before any ceremony: **approve** cascades the whole tree to
   approved and, where this Policy supersedes an approved prior, auto-deprecates that
   prior's entire tree in the same act (a fork moves the prior's governed Capabilities
   across); **reject** returns the tree to draft, back to its owner. Self-approval is
   structurally blocked — the approver must differ from the owner in subject or issuer —
   and the client says so up front rather than letting the ceremony fail.
4. **Ceremony.** Approval is a lifecycle boundary: the passkey ceremony against PS
   Service's own RP, the signing summary bound to the exact tree, its scores, and the
   digest — CUC-10's shape with a different verb. The audited actor is the approver.
5. **The item clears.** A derived item vanishes when its state resolves — resolved by
   this user, or by someone else first. Opening an already-resolved item shows the
   current state honestly ("approved by N minutes ago"), mirroring CUC-06's stale-review
   handling; the Inbox never advertises phantom work.
6. **What minimal excludes.** Human-assigned items ("you review this one"), assignment
   states, delegation, and assignment notifications all need Q-12's domain design; the
   Inbox grows into them without changing its door-role in the client.

## Artifacts

- The proposed Policy tree, read-only with scores, plus its thread — for the approval
  flow. Other items render whatever their owning CUC renders. The Inbox list itself is a
  surface of the User Pane, not an artifact.

## Approvals & notifications

- **Passkey ceremony on approve** (and see open points for reject) — the second
  lifecycle boundary after CUC-10's propose.
- Resolving clears the derived item for every viewer. No notifications are emitted in
  minimal — the draft's owner learning of approval or rejection is a Q-12/PSC-7 gap
  worth closing early.

## Contracts touched

- **PSC-7 Notifications & Inbox** — this use case *is* the contract's client half; the
  derived-items design is a candidate answer to its capability-register row that defers
  the work-item store without deferring the door.
- **PSC-1 / PSC-4** — the approver's read: full tree content and the persisted
  scorecards they are signing over.
- **PSC-6 Conversation persistence** — the document thread the approver reads; whether
  their deliberation joins it is Q-10.
- **PSC-9 / H-4** — the approve ceremony and its bound summary.
- **H-1 Tool loop** — item derivation, tree reads, and transitions as client tool calls.

## Open points

- **Q-12 remains the real design debt**: work-item states, assigner/assignee, the two
  creation sources, the relation to AccessRoles and no-self-approval, and assignment
  notifications. This CUC's derived-items Inbox is deliberately buildable without it —
  confirming that trade (ship minimal now, migrate items to records later) is the
  decision to make.
- **Reject's ceremony tier**: approve unquestionably signs; reject only returns a tree
  to draft and is undone by re-proposing. Whether reject is a signed ceremony or an
  H-4 acknowledgment is undecided — D-2's "lifecycle boundaries" covers both readings.
- **The owner hears nothing** when their proposal is approved or rejected — the
  sharpest PSC-7 notification gap the evaluator journey exposes.
- Whether the approver's questions ("why does section 3 score 2?") write into the
  document's one thread or stay in a conversation of their own — Q-10, now with a
  concrete stake.
