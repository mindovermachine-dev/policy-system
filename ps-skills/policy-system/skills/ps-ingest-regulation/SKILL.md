---
name: ps-ingest-regulation
description: Ingest an EU regulation into the Policy System compliance graph by CELEX identifier, running the full Ingestion -> Domain Mapper -> Company Merge pipeline and reporting the per-stage run summary.
---

# ps-ingest-regulation

## Purpose

Trigger a full ingestion run for one EU regulation, identified by its CELEX
number, and report the resulting per-stage run summary. This replaces
ps-cli's `ingest regulation <celex>` command (issue #126) with an
MCP-tool-backed equivalent reachable by any MCP-capable client, with no
locally-installed, signed ps-cli binary required.

**Scope:** works for a CELEX already present in the Policy System's curated
catalog (e.g. the Cyber Resilience Act, DORA, the AI Act) and for a CELEX
outside it, which is resolved against Cellar/ELI instead. Either way,
`short_name` is always required from the user — see Process step 1.

**Deliverable:** confirmation that the ingestion ran, the resolved
`regulatory_instrument_id`, and the per-stage outcome (ingestion,
extraction, derivation, merge) with each stage's own small summary — or, on
failure, the specific named error state.

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
under the right name but does not expose an `ingest_regulation` tool is
**not** a PS Service connector either, whatever it is named — report it as
unreachable (see the error-state table under Process) rather than
proceeding against it. From here on, "the PS Service connector" means the
one selected here.

## Core Principles

- Confirm both inputs (`celex`, `short_name`) with the user before calling
  the tool — this is a real, effectful action (it writes to the compliance
  graph), not a read.
- Never invent or guess a `short_name` on the user's behalf, for a curated
  or a non-curated CELEX alike. For a curated regulation, ask the user to
  supply the value they intend, and let the tool's own curated-catalog
  cross-check confirm or reject it. For a non-curated regulation, the value
  the user supplies is used exactly as given — never silently substitute a
  different value than what the user asked for.
- Never fabricate a run summary or stage outcome — report exactly what the
  tool returned.
- Never silently retry a failed call — report the named failure and stop.

## Process

1. **Confirm inputs.** Ask the user for the regulation's CELEX identifier
   (a 10-character code, e.g. `32024R2847`) and its intended `short_name` —
   `short_name` is always required, whether or not the CELEX is already in
   the curated catalog (issue #96: a non-curated ingestion never derives its
   own `short_name` from the fetched title, since doing so let two
   ingestions of the same CELEX fork into two differently-named graphs when
   the title's wording changed between fetches). Restate both back to the
   user before calling the tool.
2. **Call the tool.** Invoke `ingest_regulation` on the PS Service connector
   selected at On Load, with `celex` and `short_name` exactly as confirmed
   in step 1. This is a blocking call — a real ingestion run can take
   minutes, not seconds; do not report a failure just because the call is
   still in flight.
3. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "ingestion failed":

   | Tool result shape                                                                                    | Named state to report                                                                                                                                                                    |
   | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error                                      | "PS Service is unreachable or the caller is unauthenticated"                                                                                                                             |
   | `error: CELEX <celex> is curated under short_name '<catalog value>'; pass that value, not '<given>'` | The `short_name` given does not match this CELEX's curated value — report the catalog's actual value and ask the user whether to retry with it                                           |
   | `error: No curated regulation has CELEX '<celex>', and it does not exist on Cellar/ELI.`             | This CELEX does not exist, in the curated catalog or on Cellar/ELI — report it as not found, do not retry                                                                                |
   | `error: ingestion configuration incomplete: ...`                                                     | PS Service itself is missing a required LLM/embedding model or similarity-threshold setting — report this as a service-configuration problem, not something the user's input can fix     |
   | `error: LLM Interface is unavailable.`                                                               | The LLM Interface dependency is currently unreachable — report it distinctly from a PS Service outage; do not retry silently                                                             |
   | `error: the policy graph database is not reachable`                                                  | The compliance graph database cannot be reached — report it distinctly from an LLM Interface or transport failure                                                                        |
   | `error: <stage> stage failed: <reason>`                                                              | One pipeline stage (ingestion, extraction, derivation, or merge) genuinely failed mid-run — name the failing stage exactly as returned; earlier stages' work is not implied to be undone |
   | `error: an unexpected error occurred`                                                                | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                                                        |
   | Successful structured response                                                                       | Report `regulatory_instrument_id`, `source`, and each stage's name and summary plainly                                                                                                   |

4. **Output**, in this shape on success:

   ```text
   Ingested: <celex> as <regulatory_instrument_id>

   Stages:
     1. ingestion — <summary>
     2. extraction — <summary>
     3. derivation — <summary>
     4. merge — <summary>
   ```

   On a named error state, report that state plainly instead — do not emit
   an Output block that implies a successful run when none occurred.

## Guardrails

- Never call `ingest_regulation` without first confirming `celex` and
  `short_name` with the user — this action writes to the compliance graph.
  `short_name` has no optional or default form on this tool: issue #96 found
  that deriving it automatically from a non-curated CELEX's fetched title
  let the same regulation fork into two differently-named graphs across
  re-ingestions, so the tool always requires the user's own value instead.
- Never substitute a different `short_name` than what the user asked for,
  even when the tool reports a curated catalog mismatch — surface the
  mismatch and let the user decide.
- The skill reaches PS Service exclusively through a recognised MCP
  connector — `policy-system-graph` or `policy-system-graph-local` — never
  a direct graph connection, a repo-local script, or a spawned external
  binary.
- Never collapse the named error states in the Process table into each
  other or into a generic failure message.
- Never fabricate a run summary, a `regulatory_instrument_id`, or a stage
  outcome that the tool did not actually return.
