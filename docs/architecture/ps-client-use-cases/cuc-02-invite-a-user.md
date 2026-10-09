<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-02: Invite a user

**Status:** Specified
**Actor:** SystemAdmin or above
**Goal:** The admin leaves with a single-use invite URL for one confirmed email address,
which they deliver to the invitee themselves — no email is sent.
**Realizes:** —
**Interaction class:** Admin
**v1:** yes

## Entry points

- A new conversation: "invite pat@example.com" — in the evaluator journey, the
  first-principal session's immediate next step after CUC-01.
- The Library (CUC-11): resuming the admin conversation.

## Preconditions

- Caller is authenticated and holds `SystemAdmin` or `SystemOwner`.
- PS Service is configured with its own IdP service credential; the client never talks
  to the IdP and never holds that credential (NFR-6).

## Beats

1. **Ask.** The admin names who to invite. The AI asks for the exact email address if
   not given — never inferred, never completed from a partial address.
2. **Confirm.** The address is read back and explicitly confirmed — an H-4
   acknowledgment. The stakes are stated: an invite, once created, exists; a wrong
   address cannot be un-sent.
3. **Create.** The audit row is written before the IdP is called — if it cannot be, no
   invite is created (fail closed). On success the single-use invite URL lands in the
   conversation, with a copy affordance. Every failure keeps its name: access denied,
   authorization store unavailable (denied, fail-closed), implausible email (corrected,
   not retried blind), IdP unreachable, audit unavailable (nothing was created).
4. **Deliver.** Stated plainly, every time: **no email has been sent** — neither the
   client nor PS Service sends one. The admin delivers the URL to the invitee through a
   channel they already use.
5. **Handoff.** The invitee's first sign-in is CUC-01's invite-link path (enroll a login
   passkey, no password). The new principal arrives holding `AuthenticatedUser` alone —
   the admin's natural next step is granting roles (CUC-03).

## Artifacts

None required — the result is two fields; the conversation carries them. (A rendered
artifact would add nothing a copy affordance doesn't.)

## Approvals & notifications

- No passkey ceremony — creating an invite is an admin write behind the chat-level
  confirmation, not a lifecycle boundary.
- No Inbox items or notifications; the invitee is outside the system until they enroll.

## Contracts touched

- **H-1 Tool loop** — the invite call executes as a client tool call.
- **H-4 Permissions & approvals** — the confirm-exact-email acknowledgment.
- **PSC-6 Conversation persistence** — the admin thread persists, which is also this use
  case's sharpest open point (below).

## Open points

- **Invite URL in a persisted transcript — decided: accept it.** The audit trail
  deliberately records the invitee email but never the token or URL, yet PSC-6 persists
  both inside the server-held conversation. The URL stays in the transcript: the invite is
  single-use, bound to one email, and expires 30 minutes after creation, so an unredeemed
  URL in a persisted transcript is dead after 30 minutes and a redeemed invite is already
  dead. This is the first concrete instance of Q-11's retention question.
- **Outstanding-invite visibility — decided: v1 builds no list or revoke.** The IdP's own
  admin UI is the only place to see or revoke previously issued invites, and ps-client
  points there. An unredeemed invite lapses after 30 minutes, so a
  forgotten invite needs no cleanup; a wrong-address invite can be revoked in the IdP UI
  or left to lapse.
