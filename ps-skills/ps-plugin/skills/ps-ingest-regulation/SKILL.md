---
name: ps-ingest-regulation
description: Ingest an EU regulation into the Policy System compliance graph by CELEX identifier, submitting the full Ingestion -> Domain Mapper -> Company Merge pipeline run, poll it while narrating each stage as it starts (or report that it is already ingested), and report the per-stage run summary; if the run outlasts the skill's poll budget, report its run_id so the user can resume checking it later with "check run <run_id>".
---

# ps-ingest-regulation

## Purpose

Trigger a full ingestion run for one EU regulation, identified by its CELEX
number, keep the user informed while it runs, and report the resulting
per-stage run summary. This replaces ps-cli's `ingest regulation <celex>`
command (issue #126) with an MCP-tool-backed equivalent reachable by any
MCP-capable client, with no locally-installed, signed ps-cli binary required.

A real run takes several minutes. Rather than one silent call that only
answers at the end, the skill submits the run (`start_ingestion`, which
returns a `run_id` immediately) and then polls `get_ingestion_status`,
telling the user each time the pipeline moves on to its next stage.

**Scope:** works for any CELEX that exists on Cellar/ELI, whether or not it
is also in the Policy System's curated catalog (e.g. the Cyber Resilience Act,
DORA, the AI Act). `short_name` is always required from the user — see
Process step 1 — and is normalized to upper case (`cra` is stored as `CRA`).
A CELEX already ingested in the graph is rejected, not re-run.

**Deliverable:** confirmation that the ingestion ran, the resolved
`regulatory_instrument_id`, and the per-stage outcome (ingestion,
extraction, derivation, merge) with each stage's own small summary — or, for a
legacy celex-less node that already existed for this identifier, confirmation
that nothing new ran (issue #135's `outcome: "already_ingested"`) — or, on
failure, the specific named error state.

**Audit:** each accepted run is recorded in the audit trail as an
`ingestion_run.submit` row (before the pipeline runs) and a terminal
`ingestion_run.complete` row with the instrument id and the counts of new
Obligations, new Capabilities and matched Capabilities (or a `reason_code` on
failure). Use `ps-list-audit-events` with `details` `{"celex": "<celex>"}` to
see who ingested an instrument. If the audit trail cannot record the opening
row the run does not start.

## On Load

Exactly one connector name is recognised: `ps-mcp` ("Policy System MCP"),
the connector the Policy System Plugin declares, pointing at a hosted PS
Service. Claude lists it as `plugin:ps-plugin:ps-mcp`. Any other name is not
a PS Service connector. From here on, "the PS Service connector" means the
`ps-mcp` connector.

- If the connector exposes both `start_ingestion` and `get_ingestion_status`,
  use the submit-and-poll flow (Process steps 2-3).
- If it exposes neither of those but does expose `ingest_regulation` (an
  older PS Service), use the blocking fallback (Process step 2b) instead.
- A connector that exposes none of these three tools is **not** a PS Service
  connector, whatever it is named — report it as unreachable (see the
  error-state table under Process) rather than proceeding against it.

## Core Principles

- Confirm both inputs (`celex`, `short_name`) with the user before
  submitting — this is a real, effectful action (it writes to the compliance
  graph), not a read.
- Never invent or guess a `short_name` on the user's behalf, for a curated
  or a non-curated CELEX alike. Ask the user to supply the value they intend;
  it is used as given (normalized to upper case by the tool) — never silently
  substitute a different value than what the user asked for.
- Never fabricate a run summary, a stage, or a status — report exactly what
  the tools returned.
- A slow run is not a failed run. Never resubmit a run whose status is still
  `running`, and never silently retry a failed one — report the named
  failure and stop.
- Poll within a budget: at most **20** `get_ingestion_status` calls per
  invocation of this skill. The run keeps going on PS Service whether or not
  you are polling.
- Narrate progress, not noise: tell the user when the stage changes, not on
  every poll.

## Process

0. **Resume an earlier run.** If the user asks to check an earlier run (e.g.
   "check run <run_id>") or otherwise supplies a `run_id` from a previous
   submission, skip steps 1-2 and go straight to step 3 with that `run_id`
   and a fresh budget of 20 polls. Never call `start_ingestion` when
   resuming.
1. **Confirm inputs.** Ask the user for the regulation's CELEX identifier
   (a 10-character code, e.g. `32024R2847`) and its intended `short_name` —
   `short_name` is always required, whether or not the CELEX is in the
   curated catalog (issue #96: a non-curated ingestion never derives its
   own `short_name` from the fetched title, since doing so let two
   ingestions of the same CELEX fork into two differently-named graphs when
   the title's wording changed between fetches). Restate both back to the
   user before submitting.
2. **Submit the run.** Call `start_ingestion` on the PS Service connector
   with `celex` and `short_name` exactly as confirmed in step 1. It checks
   the request straight away and returns either
   `{"run_id": "<id>", "status": "running"}` — tell the user the run was
   submitted, and keep the `run_id` — or a string beginning `error:`, in
   which case nothing was started: classify it with the table in step 4 and
   stop.

   2b. **Fallback (older PS Service only, see On Load).** Call
   `ingest_regulation` with the same two inputs instead. This fallback is a
   blocking call that can take minutes — do not report a failure just
   because it is still in flight. Its result is either the same summary a
   succeeded run's `result` holds or an `error:` string; go straight to
   step 4/5 with it.

3. **Poll and narrate.** Call `get_ingestion_status` with that `run_id`.
   Every answer has the same shape: `{"run_id", "status", "stage",
"result", "error"}`.
   - `status: "running"` — the run is still going. If `stage` is set and
     differs from the last stage you reported, tell the user, e.g.
     "Now running: extraction (stage 2 of 4)". The stages always run in this
     order: ingestion, extraction, derivation, merge. A `stage` of `null`
     means the run is between stages or still in its opening pre-flight
     check — report nothing new. Then poll again. Space polls out (roughly
     15-30 seconds apart, when your environment gives you a way to wait);
     never resubmit while the run is still `running`.
   - `status: "succeeded"` — `result` holds the run summary; go to step 5.
   - `status: "failed"` — `error` holds the named error string; classify it
     with the table in step 4.
   - `status: "unknown"` — no run with this `run_id` exists. Report that
     the run could not be found; do not resubmit on your own.
   - A string beginning `error:` instead of an object — the status check
     itself failed; classify it with the table in step 4. Only for the
     temporarily-unavailable store error may you poll once more after a
     pause; otherwise stop.
   - Count every `get_ingestion_status` call, including the one allowed retry
     after a temporarily-unavailable store error. When 20 calls have returned
     `running`, stop polling and tell the user exactly:

     ```text
     The ingestion run is still in progress (last reported stage: <stage, or "not yet reported">).
     Run id: <run_id>
     I've stopped checking so this conversation isn't flooded — the run keeps going on PS Service.
     To pick it up later, ask me: "check run <run_id>"
     ```

     Do not report this as a failure, and do not resubmit.
4. **Name every non-success outcome**, whether it came back from
   `start_ingestion`, as a failed run's `error`, from `get_ingestion_status`
   itself, or from the fallback — never collapsed into a generic
   "ingestion failed":

   | Tool result                                                                                                  | Named state to report                                                                                                                                                                                                                                                                                        |
   | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
   | Connection/transport failure, or an auth-rejection-shaped error                                              | "PS Service is unreachable or the caller is unauthenticated"                                                                                                                                                                                                                                                 |
   | `error: this action requires a real authenticated caller`                                                    | The call carried no authenticated identity — report it as an authentication problem                                                                                                                                                                                                                          |
   | `error: You do not have the required access role for this action.`                                           | The caller lacks the `ComplianceOfficer` role needed to ingest or to check an ingestion — report it; this is not something retrying fixes                                                                                                                                                                    |
   | `error: CELEX <celex> is already ingested as short_name '<existing>'`                                        | This CELEX is already in the graph, under any `short_name` — report the existing `short_name` and that nothing new ran; do not retry under a different name                                                                                                                                                  |
   | `error: short_name '<given>' is already claimed by CELEX <other celex>`                                      | The `short_name` (compared case-insensitively) belongs to a different regulation — report the other CELEX and ask the user for a different `short_name`                                                                                                                                                      |
   | `error: CELEX '<celex>' does not exist on Cellar/ELI.`                                                       | This CELEX does not exist on Cellar/ELI — report it as not found, do not retry                                                                                                                                                                                                                               |
   | `error: ingestion configuration incomplete: ...`                                                             | PS Service itself is missing a required LLM/embedding model or similarity-threshold setting — report this as a service-configuration problem, not something the user's input can fix                                                                                                                         |
   | `error: LLM Interface is unavailable.`                                                                       | The LLM Interface dependency is currently unreachable — report it distinctly from a PS Service outage; do not retry silently                                                                                                                                                                                 |
   | `error: the policy graph database is not reachable`                                                          | The compliance graph database cannot be reached — report it distinctly from an LLM Interface or transport failure                                                                                                                                                                                            |
   | `error: too many ingestion runs are already in progress (limit <n>); wait for one to finish, then try again` | PS Service is already running its maximum number of ingestions — nothing was started; offer to submit again later, never automatically                                                                                                                                                                       |
   | `error: The ingestion run store is temporarily unavailable.`                                                 | PS Service cannot reach the store that tracks ingestion runs — from `start_ingestion` nothing was started; from `get_ingestion_status` the run itself may still be going                                                                                                                                     |
   | `error: an ingestion run for short_name '<short_name>' is already in progress; ...`                          | A run for this regulation is already going. Nothing new was started. Do not resubmit                                                                                                                                                                                                                         |
   | `error: The ingestion run could not be recorded.`                                                            | PS Service could not record the run — nothing was started                                                                                                                                                                                                                                                    |
   | `error: <stage> stage failed: <reason>`                                                                      | One pipeline stage (ingestion, extraction, derivation, or merge) genuinely failed mid-run — name the failing stage exactly as returned; earlier stages' work is not implied to be undone                                                                                                                     |
   | `error: the ingestion run was interrupted before it finished; its outcome is unknown`                        | The run stopped before finishing (e.g. PS Service restarted) — its effect on the graph is unknown; tell the user, and let them decide whether to submit again                                                                                                                                                |
   | `error: The audit trail is temporarily unavailable; the operation was not performed.`                        | The audit trail could not record the operation, so it was NOT run (no run was started) — report it distinctly from an outage of the graph or of PS Service; nothing changed                                                                                                                                  |
   | `error: an unexpected error occurred`                                                                        | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                                                                                                                                                                            |
   | `status: "succeeded"` with `result.outcome: "already_ingested"` and an empty `result.stages`                 | A legacy celex-less instrument already existed for this identifier (issue #135; a CELEX-bearing node is rejected with the already-ingested error above instead) — Domain Mapper and Company Merge did not run this time; report that the regulation is already ingested, not a fresh run's per-stage summary |
   | `status: "succeeded"` with `result.outcome: "fresh"`                                                         | Report `regulatory_instrument_id`, `source`, and each stage's name and summary plainly                                                                                                                                                                                                                       |

5. **Output**, in this shape on a fresh run (`outcome: "fresh"`):

   ```text
   Ingested: <celex> as <regulatory_instrument_id>

   Stages:
     1. ingestion — <summary>
     2. extraction — <summary>
     3. derivation — <summary>
     4. merge — <summary>
   ```

   On `outcome: "already_ingested"` (legacy celex-less node only), report instead that nothing new ran —
   never render a `Stages:` block, since none ran:

   ```text
   Already ingested: <celex> as <regulatory_instrument_id> (no changes to merge)
   ```

   On a named error state, report that state plainly instead — do not emit
   an Output block that implies a successful run when none occurred.

## Guardrails

- Never call `start_ingestion` (or the fallback `ingest_regulation`)
  without first confirming `celex` and `short_name` with the user — this
  action writes to the compliance graph. `short_name` has no optional or
  default form: issue #96 found that deriving it automatically from a
  non-curated CELEX's fetched title let the same regulation fork into two
  differently-named graphs across re-ingestions, so the tools always require
  the user's own value instead.
- Never call `start_ingestion` more than once for one confirmed request,
  and never resubmit while `get_ingestion_status` still says `running` — a
  long run is normal.
- Resuming with "check run <run_id>" only polls; it never submits a new run.
- Never report a run as failed, or as finished, while its status is still
  `running`.
- Never substitute a different `short_name` than what the user asked for,
  even when a tool reports that the name is already claimed — surface the
  collision and let the user decide.
- The skill reaches PS Service exclusively through a recognised MCP
  connector — `ps-mcp` — never a direct graph connection, a repo-local
  script, or a spawned external binary.
- Never collapse the named error states in the Process table into each
  other or into a generic failure message.
- Never fabricate a run summary, a stage, a `regulatory_instrument_id`, or
  a status that the tools did not actually return.
