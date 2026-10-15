---
name: ps-verify-audit-log
description: Check that PS Service's shared audit trail has not been altered, deleted from or truncated, by recomputing its hash chain, and report the first broken row if there is one. SystemOwner/SystemAdmin-gated, read-only.
---

# ps-verify-audit-log

## Purpose

Check the integrity of the shared, insert-only audit trail PS Service's
`ps.service.audit` component maintains. Every audit row is chained to the one
before it by a hash, and the database records the newest link as the chain
head. This skill is the MCP-tool-backed front end to `verify-audit-log`: the
database recomputes every row's hash in order and compares the last row with
the head, in one consistent snapshot, so the check is safe to run while the
service is writing. There is no other way to run it from here; the underlying
Postgres is not directly reachable by any client this skill serves.

**Scope:** one call checks the whole trail, from its first row to the head. It
reports whether the chain is intact and, if not, the first place it breaks. It
does not repair anything and does not list events — to read events, use
`ps-list-audit-events`.

**Deliverable:** an intact report with the counts the tool returned, or the
first broken row (kind, chain sequence, id and time), or the specific named
error state on failure.

## On Load

Exactly one connector name is recognised: `ps-mcp` ("Policy System MCP"),
the connector the Policy System Plugin declares, pointing at a hosted PS
Service. Claude lists it as `plugin:ps-plugin:ps-mcp`. Any other name is not
a PS Service connector. A connector that is present
under the right name but does not expose a `verify-audit-log` tool is
**not** a PS Service connector either, whatever it is named — report it as
unreachable (see the error-state table under Process) rather than
proceeding against it. From here on, "the PS Service connector" means the `ps-mcp` connector.

## Core Principles

- This is a plain read — no confirmation is needed before calling the tool;
  it writes nothing to the audit trail or any other store.
- The call needs `SystemOwner` or `SystemAdmin` and a real authenticated
  session. Say so up front if the user is unsure of their role.
- Report exactly what the tool returned. Never fabricate a row count, a
  sequence number, an id, a time, or a verdict the tool did not give.
- A break is never reported as a bare pass or fail. Always name the kind, and
  for a row break the `chain_seq`, `id` and `occurred_at`.
- Never silently retry a failed call — report the named failure and stop.

## Process

1. **Call the tool** — `verify-audit-log`, which takes no arguments — on the PS
   Service connector selected at On Load.
2. **Report the result**, distinguishing every non-success outcome into one of
   the following named states — never collapsed into a generic "verification
   failed":

   | Tool result shape                                                                                     | Named state to report                                                                                                             |
   | ----------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or `error: access-role management requires a real authenticated caller` | "PS Service is unreachable or the caller is unauthenticated" — verification is never available under the local-test bypass        |
   | `error: You do not have the required access role for this action.`                                    | `access_denied` — the caller does not hold `SystemOwner` or `SystemAdmin`                                                         |
   | `error: The authorization store is temporarily unavailable.`                                          | `authorization_store_unavailable` — the authorization or audit store cannot be reached; the check fails closed until it can be    |
   | `error: an unexpected error occurred`                                                                 | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause |
   | `intact` is `true` and `first_break` is `null`                                                        | Intact — report `rows_checked` and `head_chain_seq`                                                                               |
   | `intact` is `false` and `first_break` is set                                                          | A break — report the `first_break` as described under Output; this is a security finding, not a transient error                   |

3. **Read a break by its kind.** `first_break.kind` is one of:
   - `sequence_gap` — a row is missing: the chain sequence jumps, or the trail
     does not start at 1. `chain_seq` is the first row after the gap.
   - `prev_hash_mismatch` — the row's stored link to the previous row was
     rewritten.
   - `row_hash_mismatch` — the row's content (or time) was altered after it was
     written.
   - `head_mismatch` — every remaining row verifies, but the recorded chain
     head names a different last row: the newest rows were removed. There is
     no broken row, so only `head_chain_seq` is given. For this kind, report
     that no row id or time exists.

   The tool reports the lowest-numbered failure only; later rows may also be
   affected. Say so rather than implying the rest of the trail is sound.
   A `row_hash_mismatch` can come back with a null `occurred_at` when the time
   itself was rewritten to a value that has no timestamp; report that as it is.

4. **State what the result does not prove.** The chain is **tamper-evident**,
   not tamper-proof. It detects a changed, deleted or truncated row, but a
   database superuser or the database owner who rewrites the whole chain and
   its head consistently is not detected. An intact result therefore rules out
   partial edits, not that kind of full rewrite. Say this whenever you report
   "intact".
5. **Output**, in this shape:

   ```text
   Audit log integrity: intact
     rows checked: <rows_checked>
     chain head:   <head_chain_seq>
   ```

   or, for a row break:

   ```text
   Audit log integrity: BROKEN
     first break:  <kind>
     chain_seq:    <chain_seq>
     id:           <id>
     occurred_at:  <occurred_at, or "not available (rewritten to an infinite value)">
     rows checked: <rows_checked>
   ```

   or, for a head mismatch:

   ```text
   Audit log integrity: BROKEN
     first break:  head_mismatch (the newest rows are missing)
     chain head:   <head_chain_seq>
     rows checked: <rows_checked>
   ```

   On a named error state, report that state plainly instead — do not emit an
   Output block that implies a verification ran when none did.

6. **On a break, say what to do.** Do not attempt to fix it. Preserve the
   database as it is, compare it with the last backup, and treat it as a
   security incident (see the operations guide).

## Guardrails

- The skill reaches PS Service exclusively through a recognised MCP
  connector — `ps-mcp` — never a direct Postgres connection, a repo-local
  script, or a spawned external binary. This skill never reads the PS state
  Postgres directly and never runs SQL.
- Never call a tool other than `verify-audit-log` to produce the verdict, and
  never infer an integrity result from `list-audit-events` output.
- Never collapse the named error states into each other or into a generic
  message, and never report a break as a bare pass or fail.
- Never fabricate a `rows_checked`, `head_chain_seq`, `chain_seq`, `id`,
  `occurred_at` or `kind` value that the tool did not actually return.
- Never say the trail is tamper-proof.
