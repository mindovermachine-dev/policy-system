---
name: ps-manage-access-roles
description: List, grant, and revoke PS Service AccessRoles (SystemOwner, SystemAdmin, PolicyManager) for a principal_subject, and surface the SystemOwner floor warning, via the Policy System's RBAC/ABAC authorization framework.
---

# ps-manage-access-roles

## Purpose

List every principal's current `AccessRole` assignments, and grant or revoke
`SystemOwner`, `SystemAdmin`, or `PolicyManager` for a named
`principal_subject` — the MCP-tool-backed front end to PS Service's shared
`ps.service.authz` component (issue #133).

**Scope:** one call lists the full roster, or grants/revokes exactly one
`AccessRole` for exactly one `principal_subject` — there is no bulk grant,
no per-principal lookup by name, and `AuthenticatedUser` is never an
individually grantable or revocable target (it is a base membership fact
every already-authenticated caller has).

**Deliverable:** the full roster (`list-access-roles`) or the one
grant/revoke's own confirmation, each including the current
`system_owner_floor_warning` — or the specific named error state on
failure.

## On Load

Exactly one connector name is recognised: `ps-mcp` ("Policy System MCP"),
the connector the Policy System Plugin declares, pointing at a hosted PS
Service. Claude lists it as `plugin:ps-plugin:ps-mcp`. Any other name is not
a PS Service connector. A connector that is present
under the right name but does not expose a `list-access-roles` tool is
**not** a PS Service connector either, whatever it is named — report it as
unreachable (see the error-state table under Process) rather than
proceeding against it. From here on, "the PS Service connector" means the `ps-mcp` connector.

## Core Principles

- Confirm with the user which `principal_subject` and which `access_role`
  they want granted or revoked, and that they want to proceed, before
  calling `grant-access-role`/`revoke-access-role` — these are real,
  permanent, audited actions (they write a row to the current-state table
  and a permanent audit event) with real security consequences, not reads.
  `list-access-roles` is a plain read and needs no such confirmation.
- Never fabricate a `principal_subject`, an `access_role`, or a
  `system_owner_floor_warning` value — report exactly what the tool
  returned.
- Never guess or default a `principal_subject` the user hasn't named — if
  the user doesn't already know it, call `list-access-roles` first (or ask
  them) rather than inventing one.
- When a response's `system_owner_floor_warning` is `true`, report it
  prominently — it means exactly one active `SystemOwner` currently exists,
  a real single-point-of-failure the user should know about.
- Never silently retry a failed call — report the named failure and stop.

## Process

1. **Identify the target, if granting or revoking.** If the user doesn't
   already know the exact `principal_subject` to act on, call
   `list-access-roles` first and let them pick from the returned roster.
2. **Confirm before an effectful call.** For `grant-access-role` or
   `revoke-access-role`, confirm the exact `principal_subject` and
   `access_role` with the user, and that they want to proceed, before
   calling the tool.
3. **Call the tool** — `list-access-roles` (no arguments),
   `grant-access-role`, or `revoke-access-role` (each with
   `principal_subject` and `access_role: "SystemOwner" | "SystemAdmin" |
"PolicyManager"`) — on the PS Service connector selected at On Load.
4. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "action failed":

   | Tool result shape                                                                                     | Named state to report                                                                                                                                 |
   | ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or `error: access-role management requires a real authenticated caller` | "PS Service is unreachable or the caller is unauthenticated" — access-role management is never available under the local-test bypass                  |
   | `error: You do not have the required access role for this action.`                                    | `access_denied` — the caller does not hold the role this action requires                                                                              |
   | `error: The requested access role is not recognized.`                                                 | `invalid_access_role` — `access_role` is not one of `SystemOwner`/`SystemAdmin`/`PolicyManager`, or not one this tool manages                         |
   | `error: You cannot grant or revoke your own access roles.`                                            | `self_grant_revoke_blocked` — the caller targeted their own `principal_subject`                                                                       |
   | `error: This action would leave zero active SystemOwners.`                                            | `system_owner_floor_violation` — revoking would leave no active `SystemOwner`; report this distinctly, it is a deliberate safety rejection, not a bug |
   | `error: The authorization store is temporarily unavailable.`                                          | `authorization_store_unavailable` — the authorization store cannot be reached; every role-gated action fails closed until it is                       |
   | `error: an unexpected error occurred`                                                                 | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                     |
   | Successful structured response                                                                        | Report the roster or the one grant/revoke's own confirmation, including `system_owner_floor_warning`                                                  |

5. **Output**, in this shape on success:

   For `list-access-roles`:

   ```text
   AccessRole assignments:
     <principal_subject> (<principal_issuer>) — <access_role>, granted by <granted_by_subject> at <granted_at>
     ...

   SystemOwner floor warning: <true|false>
   ```

   For `grant-access-role`/`revoke-access-role`:

   ```text
   <Granted|Revoked>: <access_role> for <principal_subject>, by <granted_by_subject|revoked_by_subject>

   SystemOwner floor warning: <true|false>
   ```

   On a named error state, report that state plainly instead — do not emit
   an Output block that implies a successful action when none occurred.

## Guardrails

- The skill reaches PS Service exclusively through a recognised MCP
  connector — `ps-mcp` — never
  a direct graph connection, a repo-local script, or a spawned external
  binary.
- Never call `grant-access-role`/`revoke-access-role` without the user
  having named the exact `principal_subject` and `access_role`, and
  confirmed they want to proceed.
- Never collapse the named error states into each other or into a generic
  message.
- Never fabricate a `principal_subject`, `access_role`,
  `granted_by_subject`/`revoked_by_subject`, `granted_at`, or
  `system_owner_floor_warning` value that the tool did not actually return.
