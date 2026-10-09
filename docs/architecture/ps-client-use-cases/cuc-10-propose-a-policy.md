<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-10: Propose a policy

**Status:** Specified
**Actor:** The draft's owner (propose is owner-only; no role is required)
**Goal:** A finished draft Policy tree moved `draft` → `proposed` through a deliberate,
signed user action, the transition audited, the tree now awaiting another person's
approve/reject.
**Realizes:** UC-2
**Interaction class:** Lifecycle
**v1:** yes

A clickable mockup of this use case (together with CUC-08 and CUC-09) exists at
`../ps-client-mockup/index.html`.

## Entry points

- The end of CUC-09's authoring flow, in the document's own thread.
- The Library's document list (CUC-11): resuming a passing draft days later and proposing
  it then.

## Preconditions

- Caller is authenticated and owns the draft — `propose-policy` is owner-only.
- The Policy has at least one Standard attached (the server's completeness gate).
- Every document in the tree clears its rubric `pass_threshold` — the design-level
  readiness bar; whether the server also enforces it is open (see below).

## Beats

1. **Readiness.** CUC-09's end state: all documents pass, the AI confirms the holistic
   rescore and notes the tree is ready to propose. Chat suggests; chat never transitions.
2. **The click.** The user clicks **Propose** on the document in the Artifact Pane — the
   lifecycle control lives on the artifact, and the click is the user's deliberate act.
3. **Signing summary.** Before any ceremony, the client shows exactly what will be signed:
   the full tree (Policy, its Standards, their Controls), each document's final rubric
   score, and a content digest. The user reviews; cancel is always available and costless.
4. **Passkey ceremony.** The transaction-signing ceremony runs against PS Service's own
   WebAuthn relying party — distinct from the login passkey at the OIDC IdP, one familiar
   gesture to the user.
5. **Transition.** `draft` → `proposed` applies to the Policy and every Standard/Control
   in its tree in one write; the event is audited. The pane reflects the new status
   immediately: editing affordances disappear (writes are draft-status-gated), badges and
   content stay visible as the signed record.
6. **Aftermath.** The proposal is recorded in the document's continuous thread. What
   follows belongs to other actors and use cases: approval requires a `PolicyManager` who
   is not the owner (CUC-12 when it arrives as an assignment); a rejection or the owner's
   own revert returns the tree to `draft` and re-enters CUC-09.

## Artifacts

- The Policy tree — until now an editable rubric-scored draft — rendered read-only as
  `proposed`. The signing summary is the H-4 approval surface, not an artifact tab.

## Approvals & notifications

- **The passkey ceremony at the propose boundary** — the defining instance of the
  lifecycle-only ceremony rule: binding covers the exact tree, its final scores, and the
  digest.
- No notifications or Inbox items are minted today; whether proposing creates an approver
  work item is Q-12 territory.

## Contracts touched

- **H-1 Tool loop** — the transition executes as a client tool call against PS Service.
- **H-4 Permissions & approvals** — the signing-summary preview and acknowledgment,
  independent of model choice.
- **PSC-1 Document read** — the summary and the post-transition pane both render the full
  final tree.
- **PSC-4 Scoring** — the final per-document scores bound into the signing summary must
  come from somewhere authoritative.
- **PSC-5 Document sync** — the status change becomes visible to any other open surface
  without manual refresh.
- **PSC-6 Conversation persistence** — the proposal lands in the document's governance
  thread.
- **PSC-9 Auth flows** — the ceremony against PS Service's own RP, embeddable in the
  client (SP-4 is its evidence).

## Open points

- Whether the server gates `propose-policy` on `pass_threshold` or readiness stays
  advisory. Today it cannot gate: no server-side scoring exists (Q-13), which also means
  the signing summary's "final scores" have no server-verified source yet.
- Server-side ceremony enforcement: the danger-zone cleanup endpoints demand a passkey
  approval before executing; the lifecycle endpoints do not yet. Embedding the ceremony
  in the client (PSC-9) is only half the contract.
- The digest: what it covers and who computes it (client, server, or both and compared).
- Q-11 — whether the document's thread freezes when the document leaves `draft`.
- Q-12 — whether proposing mints an Inbox assignment for eligible approvers.
