---
name: ps-restore-instrument
description: Fetch one curated instrument's artifact (manifest, baseline, native) from the Policy System's configured curated-content source and restore it into the compliance graph, given its instrument_id.
---

# ps-restore-instrument

## Purpose

Fetch and restore one curated instrument's artifact straight from the
effective curated-content source — no locally-installed, signed ps-cli
binary or local checkout required. This replaces ps-cli's `restore
instrument` command (issue #127) with an MCP-tool-backed equivalent
reachable by any MCP-capable client.

**Scope:** one call restores exactly one instrument, named by its
`instrument_id` (e.g. `CRA-1.0`) — normally discovered first via
`ps-get-catalog-listing`. There is no bulk/multi-instrument restore.

**Deliverable:** the restored `instrument_id` and one `stage`/`status`
entry per completed restore stage — the same summary ps-cli's own `restore
instrument` used to print — or the specific named error state on failure.

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
under the right name but does not expose a `restore_instrument` tool is
**not** a PS Service connector either, whatever it is named — report it as
unreachable (see the error-state table under Process) rather than
proceeding against it. From here on, "the PS Service connector" means the
one selected here.

## Core Principles

- Confirm with the user which `instrument_id` they want restored, and that
  they want to proceed, before calling the tool — this is a real, effectful
  action (it writes the instrument's native, baseline, and single-tenant
  graph content), not a plain read. Restoring is documented idempotent
  (re-restoring the same instrument overwrites its own prior content via
  the same rename-based finalize) — this is not an irreversible action like
  a near-miss merge, so no elicitation/confirmation round trip from the
  tool itself is expected; the confirmation asked for here is this skill's
  own guardrail, not a substitute for naming the right `instrument_id`.
- Never fabricate an `instrument_id`, a stage, or a status — report exactly
  what the tool returned.
- Never guess or default an `instrument_id` the user hasn't named — if it
  isn't already known, call `ps-get-catalog-listing` first (or ask the
  user) rather than inventing one.
- Never silently retry a failed call — report the named failure and stop.

## Process

1. **Confirm the instrument.** Ask the user which `instrument_id` to
   restore, and confirm they want to proceed. If they don't already know
   the exact id, call `ps-get-catalog-listing` first and let them pick from
   the returned listing.
2. **Call the tool.** Invoke `restore_instrument` with `instrument_id` on
   the PS Service connector selected at On Load.
3. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "restore failed":

   | Tool result shape                                               | Named state to report                                                                                                                                     |
   | --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error | "PS Service is unreachable or the caller is unauthenticated"                                                                                              |
   | A schema-validation rejection (a malformed `instrument_id`)     | "instrument_id is not well-formed" — report the id that was rejected; never retry with a guessed correction                                               |
   | `error: <message naming the curated-content source>`            | The configured curated-content source is unreachable, or the fetched artifact is missing/malformed — report the message verbatim                          |
   | `error: <message about a checksum/schema_version mismatch>`     | "Fetched artifact rejected (checksum or schema_version mismatch)" — the artifact itself failed integrity verification, distinct from a source outage      |
   | `error: <message naming a failing restore stage>`               | "Restore stage failed" — report the message verbatim, including a missing similarity-threshold configuration failure, distinct from an artifact rejection |
   | `error: the policy graph database is not reachable`             | The compliance graph database cannot be reached — report it distinctly from every state above                                                             |
   | `error: an unexpected error occurred`                           | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                         |
   | Successful structured response                                  | Report `instrument_id` and every completed stage's own `stage`/`status` plainly                                                                           |

4. **Output**, in this shape on success:

   ```text
   Restored: <instrument_id>

   Stages:
     <stage> — <status>
     ...
   ```

   On a named error state, report that state plainly instead — do not emit
   an Output block that implies a successful restore when none occurred.

## Guardrails

- The skill reaches PS Service exclusively through a recognised MCP
  connector — `policy-system-graph` or `policy-system-graph-local` — never
  a direct graph connection, a repo-local script, or a spawned external
  binary. This tool never reads a local curated-content checkout.
- Never call `restore_instrument` without the user having named which
  `instrument_id` to restore and confirmed they want to proceed.
- Never collapse the named error states into each other or into a generic
  message.
- Never fabricate an `instrument_id`, a stage, or a status value that the
  tool did not actually return.
