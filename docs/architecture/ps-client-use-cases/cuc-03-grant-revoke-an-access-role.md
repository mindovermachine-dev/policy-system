<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-03: Grant/revoke an access role

**Status:** Specified
**Actor:** SystemAdmin or above; the tiering below decides which grants each may make
**Goal:** One principal granted or revoked one AccessRole — permanent, audited, with the
SystemOwner floor protected and its warning surfaced.
**Realizes:** —
**Interaction class:** Admin
**v1:** yes

## Entry points

- A new conversation: "make Pat a PolicyManager" — in the evaluator journey, the step
  after an invitee's first login (CUC-01 via CUC-02).
- The Library (CUC-11): resuming the admin conversation.

## Preconditions

- Caller is authenticated; listing the roster requires `SystemAdmin` or `SystemOwner`.
- Grants are tiered: granting `SystemAdmin` or `SystemOwner` requires the caller hold
  `SystemOwner`; granting `PolicyManager` or `ComplianceOfficer` requires `SystemAdmin`
  or above. Four roles are grantable; `AuthenticatedUser` is never one — it is the base
  fact of being authenticated, not an assignment.

## Beats

1. **Survey.** The roster renders as a read-only report: every assignment with who
   granted it and when, and the `system_owner_floor_warning` — when `true` (exactly one
   active SystemOwner exists), stated prominently as the single point of failure it is,
   not as a footnote.
2. **Choose.** The admin names the exact `principal_subject` and role — picked from the
   roster or supplied by the admin, never guessed or completed. Direction (grant or
   revoke) is explicit.
3. **Confirm.** An H-4 acknowledgment restates principal, role, and direction: this is a
   permanent, audited write with security consequences, not a read.
4. **Execute.** The grant or revoke lands with its audit event; the response's floor
   warning is surfaced again. Refusals keep their names and are explained as design, not
   failure: self-grant/self-revoke blocked (a SystemOwner cannot revoke their own
   `SystemOwner` either), the deliberate rejection of any revoke that would leave zero
   active SystemOwners, tier violations as access denied, authorization store
   unavailable as fail-closed denial.
5. **Effect.** The target's capabilities change from their next call onward. Nothing
   tells them — see open points.

## Artifacts

- The roster: a read-only report (non-editable, non-scored). Grant/revoke confirmations
  stay in chat — two fields, as in CUC-02.

## Approvals & notifications

- No passkey ceremony today; the chat-level confirmation is the only gate (see open
  points for whether that holds for owner-tier grants).
- No notification to the affected principal; no Inbox items.

## Contracts touched

- **H-1 Tool loop** — roster read and grant/revoke as client tool calls.
- **H-4 Permissions & approvals** — the restate-and-confirm step before a permanent
  security write.
- **PSC-6 Conversation persistence** — the admin thread as the informal record beside
  the audit trail.
- **PSC-10 Artifact type registry** — the roster report's flags.

## Open points

- **Discovering a new principal.** `AuthenticatedUser` is a service-layer default, not a
  stored row — a freshly enrolled invitee appears nowhere in the roster until their
  first grant. The admin must obtain the `principal_subject` out-of-band (from the
  invitee, or the IdP's admin UI); CUC-02 → CUC-03 therefore has a gap in the middle
  that the client cannot bridge today. Candidates: a "principals seen" read, or carrying
  the subject through the invite. Related to PSC-12 (CUC-01's self-grants read), which covers the caller's own
  grants, not other principals.
- **Ceremony tier for owner-level grants.** Granting `SystemOwner` is arguably the most
  security-consequential single write in the system, yet it rides on chat confirmation
  while graph merges demand a passkey. Role writes are reversible (revoke exists), which
  argues for the lighter gate — but the asymmetry deserves a deliberate decision, not a
  default.
- **The grantee learns nothing.** A grant or revoke changes what a person can do with no
  signal to them — a natural PSC-7 notification once that model exists.
