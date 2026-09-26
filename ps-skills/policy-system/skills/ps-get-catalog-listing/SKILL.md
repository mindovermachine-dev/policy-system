---
name: ps-get-catalog-listing
description: List every curated instrument (external and internal) available from the Policy System's configured curated-content source, so a user or another skill can discover an instrument_id before restoring it.
---

# ps-get-catalog-listing

## Purpose

Return the full curated instrument listing PS Service currently serves,
straight from the effective curated-content source — no locally-installed,
signed ps-cli binary or local checkout required. This replaces ps-cli's
`get catalog` command (issue #127) with an MCP-tool-backed equivalent
reachable by any MCP-capable client.

**Scope:** one call returns every curated instrument, external and
internal, unfiltered — there is no per-instrument lookup or search
parameter.

**Deliverable:** the `instruments` list the tool returned, each entry's own
`instrument_id`, `title`, `source_type`, and `jurisdiction` (`None` for an
internal-source entry) — or the specific named error state on failure.

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
under the right name but does not expose a `get-catalog-listing` tool is
**not** a PS Service connector either, whatever it is named — report it as
unreachable (see the error-state table under Process) rather than
proceeding against it. From here on, "the PS Service connector" means the
one selected here.

## Core Principles

- This is a plain read — no confirmation is needed before calling the
  tool; it has no side effect on the compliance graph.
- Never fabricate an instrument entry — report exactly what the tool
  returned, for every instrument, not just the ones relevant to whatever
  the user asked about.
- Never silently retry a failed call — report the named failure and stop.

## Process

1. **Call the tool.** Invoke `get-catalog-listing` (no arguments) on the PS
   Service connector selected at On Load.
2. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "listing failed":

   | Tool result shape                                               | Named state to report                                                                                                                                                                       |
   | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error | "PS Service is unreachable or the caller is unauthenticated"                                                                                                                                |
   | `error: <message naming the curated-content source>`            | The configured curated-content source is unreachable or returned a missing/malformed `catalog.json` — report the message verbatim, distinct from the generic unexpected-failure state below |
   | `error: an unexpected error occurred`                           | An unrecognised failure — report it as an unexpected error, distinct from the source-unreachable state above; never guess at its cause                                                      |
   | Successful structured response                                  | Report every instrument's `instrument_id`, `title`, `source_type`, and `jurisdiction` plainly                                                                                               |

3. **Output**, in this shape on success:

   ```text
   Curated instruments:
     <instrument_id> — <title> (<source_type>[, <jurisdiction>])
     ...
   ```

   On a named error state, report that state plainly instead — do not emit
   an Output block that implies a successful listing when none occurred.

## Guardrails

- The skill reaches PS Service exclusively through a recognised MCP
  connector — `policy-system-graph` or `policy-system-graph-local` — never
  a direct graph connection, a repo-local script, or a spawned external
  binary. This tool never reads a local curated-content checkout.
- Never collapse the named error states into each other or into a generic
  message.
- Never fabricate an `instrument_id`, `title`, `source_type`, or
  `jurisdiction` value that the tool did not actually return.
