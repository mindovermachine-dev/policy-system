---
name: ps-invite-user
description: Invite a new user by creating a single-use Authentik enrollment invite via PS Service's invite-user MCP tool. SystemAdmin-or-above only.
---

# ps-invite-user

## Purpose

Create a single-use Authentik enrollment invite for a named target email —
the MCP-tool-backed front end to PS Service's own `PS_AUTHENTIK_API_TOKEN`
service credential, replacing the manual admin curl call against
Authentik's invitation-stage API (issue #140).

**Scope:** one call creates exactly one single-use invite for exactly one
target email — there is no bulk invite, no lookup of previously issued
invites, and no email is ever sent by this skill or by PS Service; the
resulting URL must be delivered to the invitee by the admin themselves.

**Deliverable:** the created invite's `itoken` and `invite_url` — or the
specific named error state on failure.

## On Load

Exactly one connector name is recognised: `ps-mcp` ("Policy System MCP"),
the connector the Policy System Plugin declares, pointing at a hosted PS
Service. Claude lists it as `plugin:ps-plugin:ps-mcp`. Any other name is not
a PS Service connector. A connector that is present
under the right name but does not expose an `invite-user` tool is **not** a
PS Service connector either, whatever it is named — report it as
unreachable (see the error-state table under Process) rather than
proceeding against it. From here on, "the PS Service connector" means the `ps-mcp` connector.

## Core Principles

- **Confirm the exact target email with the user before calling
  `invite-user`.** This is a real, audited, effectful action — it creates a
  live single-use invite against Authentik and records a permanent audit
  entry — not a read. A wrong invite email cannot be silently un-sent: once
  created, the invite exists and its URL may already be in the admin's
  hands, so there is no "undo" to fall back on if the address was wrong.
  Never proceed on an inferred, guessed, or partially-typed address.
- Never fabricate an `itoken` or `invite_url` — report exactly what the
  tool returned.
- Never silently retry a failed call — report the named failure and stop.
- This skill never sends email and has no capability to do so. On success,
  the admin — not the skill, not PS Service — must deliver the
  `invite_url` to the invitee through whatever channel they already use.

## Process

1. **Confirm the target email.** Ask the user for the exact email address
   to invite, and read it back to them for confirmation before proceeding.
   Do not continue on an address the user has not explicitly confirmed.
2. **Call the tool** — `invite-user` with `email` set to the confirmed
   address — on the PS Service connector selected at On Load.
3. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "invite failed":

   | Tool result shape                                                                          | Named state to report                                                                                                              |
   | ------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or `error: this action requires a real authenticated caller` | "PS Service is unreachable or the caller is unauthenticated"                                                                       |
   | `error: You do not have the required access role for this action.`                         | `access_denied` — the caller does not hold `SystemAdmin` or above                                                                  |
   | `error: The authorization store is temporarily unavailable.`                               | `authorization_store_unavailable` — the authorization store cannot be reached; the action fails closed until it is                 |
   | The tool call itself is rejected before running (`email` is not a plausible address)       | `invalid_email` — the address given is not a plausible email; ask the user to confirm/correct it and re-check before retrying      |
   | `error: Authentik invitation request failed: ...`                                          | `authentik_unavailable` — Authentik is unreachable or returned a non-2xx response; report the state, not the raw message internals |
   | `error: an unexpected error occurred`                                                      | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause  |
   | Successful structured response                                                             | Report `itoken` and `invite_url` plainly                                                                                           |

4. **Output**, in this shape on success:

   ```text
   Invite created for <email>:
     itoken: <itoken>
     invite_url: <invite_url>

   No email has been sent — deliver this URL to <email> yourself.
   ```

   On a named error state, report that state plainly instead — do not emit
   an Output block that implies a successful invite when none occurred.

## Guardrails

- The skill reaches PS Service exclusively through a recognised MCP
  connector — `ps-mcp` — never
  a direct Authentik connection, a repo-local script, or a spawned external
  binary.
- Never call `invite-user` without the user having explicitly confirmed the
  exact email address to invite.
- Never fabricate an `itoken` or `invite_url` that the tool did not
  actually return.
- Never claim or imply that an email was sent to the invitee — no such
  capability exists here; this matches the manual curl workflow this skill
  replaces, which never sent email either (`docs/artifacts/
idp-configuration-contract.md`'s "Verify" section).
- Never collapse the named error states into each other or into a generic
  message.
