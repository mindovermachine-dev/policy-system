---
name: ps-check-regulations
description: Sweep every regulation tracked in the Policy System compliance graph for detected amendments, automatically re-ingesting any that are found, and report the per-instrument outcome.
---

# ps-check-regulations

## Purpose

Trigger one change-check sweep across every actively-tracked external
regulation/directive in the compliance graph, and report each tracked
instrument's outcome. This replaces ps-cli's `check regulations` command
(issue #126) with an MCP-tool-backed equivalent reachable by any
MCP-capable client, with no locally-installed, signed ps-cli binary
required.

**Scope:** one sweep call covers every tracked instrument in one run — there
is no per-instrument selection; a detected amendment is re-ingested
automatically as part of the same sweep, not as a separate confirmation
step.

**Deliverable:** the sweep's `run_id` and, for every tracked instrument, its
own outcome — `current`, `amendment_reingested`, `poll_failed`,
`not_configured`, `skipped`, or `reingest_failed` — with whatever
`detail`/`reingest_run_id` the tool returned, or the specific named error
state on failure.

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
under the right name but does not expose a `check_regulations` tool is
**not** a PS Service connector either, whatever it is named — report it as
unreachable (see the error-state table under Process) rather than
proceeding against it. From here on, "the PS Service connector" means the
one selected here.

## Core Principles

- Confirm with the user that they want to run a full sweep before calling
  the tool — this is a real, effectful action (a detected amendment is
  re-ingested automatically, writing to the compliance graph), not a plain
  read.
- Never fabricate a sweep result or per-instrument outcome — report exactly
  what the tool returned, for every tracked instrument, not just the ones
  with something noteworthy.
- Never collapse the six outcome buckets into a summary like "N regulations
  checked" without also naming which instrument landed in which bucket.
- Never silently retry a failed call — report the named failure and stop.

## Process

1. **Confirm intent.** Ask the user to confirm they want to sweep every
   tracked regulation now — note that a detected amendment triggers an
   automatic re-ingestion as part of the same call, not a separate,
   confirmable step.
2. **Call the tool.** Invoke `check_regulations` (no arguments) on the PS
   Service connector selected at On Load. This is a blocking call — a sweep
   that re-ingests one or more detected amendments can take minutes, not
   seconds; do not report a failure just because the call is still in
   flight.
3. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "check failed":

   | Tool result shape                                               | Named state to report                                                                                                                  |
   | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error | "PS Service is unreachable or the caller is unauthenticated"                                                                           |
   | `error: LLM Interface is unavailable.`                          | The LLM Interface dependency is currently unreachable — report it distinctly from a PS Service outage; do not retry silently           |
   | `error: the policy graph database is not reachable`             | The compliance graph database cannot be reached — report it distinctly from an LLM Interface or transport failure                      |
   | `error: an unexpected error occurred`                           | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause      |
   | Successful structured response                                  | Report `run_id`, then every tracked instrument's own `instrument_id` and outcome plainly — see step 4's per-instrument reporting rules |

   For a successful response, report each tracked instrument's outcome
   distinctly — never collapse the six buckets into each other or into one
   generic "checked":

   | Outcome                | What it means                                                                                           | What to report                                                   |
   | ---------------------- | ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
   | `current`              | No newer consolidated version was found                                                                 | State the instrument is up to date                               |
   | `poll_failed`          | Polling this instrument's source failed                                                                 | Report as a poll failure, not "up to date" or "no amendment"     |
   | `not_configured`       | This instrument has no poll source configured                                                           | Report as unconfigured, distinct from a poll failure             |
   | `amendment_reingested` | An amendment was detected and successfully re-ingested                                                  | Report the `detail` and `reingest_run_id` the tool returned      |
   | `skipped`              | Re-ingestion was skipped (e.g. a national-transposition instrument, which this tool does not re-ingest) | Report the `detail` the tool returned, naming why it was skipped |
   | `reingest_failed`      | An amendment was detected but re-ingestion failed                                                       | Report the `detail` the tool returned as the failure reason      |

4. **Output**, in this shape on success:

   ```text
   Swept: run <run_id>

   Instruments:
     <instrument_id> — <outcome>[: <detail>]
     ...
   ```

   On a named error state, report that state plainly instead — do not emit
   an Output block that implies a successful sweep when none occurred.

## Guardrails

- Never call `check_regulations` without first confirming with the user
  that a full sweep (including any automatic re-ingestion it triggers) is
  what they want.
- The skill reaches PS Service exclusively through a recognised MCP
  connector — `policy-system-graph` or `policy-system-graph-local` — never
  a direct graph connection, a repo-local script, or a spawned external
  binary.
- Never collapse the named error states or the six per-instrument outcome
  buckets into each other or into a generic message.
- Never fabricate a `run_id`, an outcome, or a `detail`/`reingest_run_id`
  value that the tool did not actually return.
