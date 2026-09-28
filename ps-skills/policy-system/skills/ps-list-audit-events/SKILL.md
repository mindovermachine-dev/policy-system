---
name: ps-list-audit-events
description: Read PS Service's shared audit_events trail — who did what, to what, and when — filtered by actor, resource, action, or time range, and paginated newest-first. SystemOwner/SystemAdmin-gated, read-only.
---

# ps-list-audit-events

## Purpose

Read the shared, insert-only `audit_events` trail PS Service's `ps.service.audit`
component maintains — every access-role bootstrap/grant/revoke (applied or
denied) today, with more actions (policy lifecycle, supersede-fork,
`user.invite`) joining it over time as each feature adopts the same store
(issue #147). The MCP-tool-backed front end to `list-audit-events` — there is
no other read path; the underlying Postgres is not directly reachable by any
client this skill serves.

**Scope:** one call returns one filtered, newest-first page of events — there
is no bulk export and no cross-page aggregation performed by this skill
itself; each of `actor_subject`, `actor_issuer`, `resource_type`,
`resource_id`, `action`, `occurred_from`, `occurred_to` is optional and
independently combinable, and `cursor`/`page_size` control paging through
however many pages the caller wants to walk.

**Deliverable:** the matching `events` for the requested filters/page,
newest-first, plus whether a further page remains (`next_cursor`) — or the
specific named error state on failure.

## On Load

Two connector names are recognised, and exactly one of them is expected to
be present in any given session:

- `policy-system-graph` — the connector this plugin declares, pointing at a
  hosted PS Service.
- `policy-system-graph-local` — a user-registered local MCP server bridging
  to a PS Service running on the same machine (local test; see the user
  guide's step 8).

Use whichever is present; if both are, prefer `policy-system-graph` and
fall back to `policy-system-graph-local` only if the former is unreachable.
Any other name is not a PS Service connector. A connector that is present
under the right name but does not expose a `list-audit-events` tool is
**not** a PS Service connector either, whatever it is named — report it as
unreachable (see the error-state table under Process) rather than
proceeding against it. From here on, "the PS Service connector" means the
one selected here.

## Core Principles

- This is a plain read — no confirmation is needed before calling the tool;
  it has no side effect on `audit_events` or any other store. Unlike
  `ps-manage-access-roles`'s grant/revoke calls, there is nothing here to
  confirm before acting.
- Never fabricate an event, a filter value, or a `next_cursor` — report
  exactly what the tool returned.
- Never guess or invent an `action` or `resource_type` filter value the user
  hasn't named — if they want to filter by one but don't know the exact
  string (e.g. `access_role.grant`, `principal`), ask them or run one
  unfiltered call first and let them narrow from what comes back.
- `cursor` is opaque — pass back exactly the `next_cursor` a prior call
  returned to advance to the next page; never construct or guess one.
- Never silently retry a failed call — report the named failure and stop.

## Process

1. **Gather filters, if any.** Ask the user what they want to narrow by —
   actor, resource, action, or a time range — or confirm they want the
   unfiltered, most-recent events. All filters are optional and combinable.
2. **Call the tool** — `list-audit-events`, with whichever of
   `actor_subject`, `actor_issuer`, `resource_type`, `resource_id`,
   `action`, `occurred_from`, `occurred_to` (ISO 8601), `cursor`, and
   `page_size` (default 25, maximum 100) the user supplied — on the PS
   Service connector selected at On Load.
3. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "listing failed":

   | Tool result shape                                                                                     | Named state to report                                                                                                                |
   | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
   | Connection/transport failure, or `error: access-role management requires a real authenticated caller` | "PS Service is unreachable or the caller is unauthenticated" — audit-trail access is never available under the local-test bypass     |
   | `error: You do not have the required access role for this action.`                                    | `access_denied` — the caller does not hold `SystemOwner` or `SystemAdmin`                                                            |
   | `error: The 'action' filter names an action that is not registered.`                                  | `invalid_action_filter` — the `action` string given isn't a registered action; ask the user to confirm the exact spelling            |
   | `error: The 'resource_type' filter names a resource type that is not registered.`                     | `invalid_resource_type_filter` — the `resource_type` string given isn't registered                                                   |
   | `error: The 'occurred_from' filter must not be later than 'occurred_to'.`                             | `invalid_time_range_filter` — the time range is backwards; ask the user to confirm the two bounds                                    |
   | `error: The 'cursor' filter is malformed.`                                                            | `invalid_cursor_filter` — never happens from a `next_cursor` this skill passed back unmodified; if seen, start over with no `cursor` |
   | `error: The 'page_size' filter must not exceed 100.`                                                  | `invalid_page_size_filter` — ask for a smaller page size (≤ 100)                                                                     |
   | `error: The authorization store is temporarily unavailable.`                                          | `authorization_store_unavailable` — the authorization store cannot be reached; every role-gated action fails closed until it is      |
   | `error: an unexpected error occurred`                                                                 | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause    |
   | Successful structured response                                                                        | Report the returned `events`, newest first, and whether `next_cursor` means more remain                                              |

4. **Offer the next page, if any.** When `next_cursor` is not `None`, tell
   the user more events remain and ask whether to continue before calling
   `list-audit-events` again with that exact `cursor` value.
5. **Output**, in this shape on success:

   ```text
   Audit events (newest first):
     <occurred_at> — <action> on <resource_type>:<resource_id> by <actor_subject> (<actor_issuer>) — <outcome>
       details: <details>
     ...

   More events available: <yes, pass cursor "<next_cursor>" | no>
   ```

   On a named error state, report that state plainly instead — do not emit
   an Output block that implies a successful listing when none occurred.

## Guardrails

- The skill reaches PS Service exclusively through a recognised MCP
  connector — `policy-system-graph` or `policy-system-graph-local` — never
  a direct Postgres connection, a repo-local script, or a spawned external
  binary. This tool never reads the authz Postgres directly.
- Never call `list-audit-events` with a fabricated or guessed `cursor` —
  only ever a `next_cursor` value returned by a prior call on the same
  filter set.
- Never collapse the named error states into each other or into a generic
  message.
- Never fabricate an `id`, `occurred_at`, `actor_subject`, `actor_issuer`,
  `action`, `resource_type`, `resource_id`, `outcome`, `details`, or
  `next_cursor` value that the tool did not actually return.
