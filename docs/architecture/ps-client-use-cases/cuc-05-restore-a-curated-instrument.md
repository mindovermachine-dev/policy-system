<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-05: Restore a curated instrument

**Status:** Specified
**Actor:** Compliance Officer (`restore_instrument` is gated on minimum
`ComplianceOfficer`)
**Goal:** One curated instrument — external regulation or internal
(e.g. engineering practices) — fetched from the configured curated-content source and
restored into the compliance graph, with the outcome persisted as a run record.
**Realizes:** UC-1
**Interaction class:** Long-running run
**v1:** yes

This use case shares CUC-04's run-artifact shape; only the differences are spelled out
here.

## Entry points

- A new conversation: "restore DORA from the catalog," or just "what's in the catalog?"
- The Library (CUC-11): reopening the conversation or its artifacts later.

## Preconditions

- Caller is authenticated; the restore itself requires the `ComplianceOfficer` grant.
- A curated-content source is configured and reachable; the instrument exists in it.
- Restore is idempotent by design — re-restoring an instrument overwrites that
  instrument's own prior content, so an already-restored instrument is not a blocker the
  way an already-ingested CELEX is in CUC-04.

## Beats

1. **Discover.** If the user doesn't already know the `instrument_id`, the catalog
   listing renders as a **read-only report** in the Artifact Pane — every curated
   instrument, external and internal, with its id. The user picks from the report; an id
   is never guessed or fabricated on their behalf.
2. **Confirm.** The AI restates the chosen `instrument_id` and that restoring writes the
   instrument's native, baseline, and graph content — an H-4 acknowledgment in chat.
   Idempotence is stated, not assumed silently, when the instrument is already in the
   graph.
3. **Run.** The restore executes; an audit row opens before anything is restored — if the
   audit trail cannot record it, nothing is restored (fail closed). A **run artifact**
   opens, as in CUC-04. Unlike ingestion, today's restore is a single blocking server
   call: the artifact may show stages only as the terminal record rather than live
   transitions (see open points).
4. **Terminal state.** On success the artifact persists as the record: the restored
   `instrument_id` and each completed stage with its status. Every failure surfaces as
   its specific named state — source unreachable, artifact rejected on
   checksum/schema-version, a named failing stage, graph unreachable, audit unavailable
   (nothing ran) — never a generic "restore failed," never silently retried.
5. **Consequences.** The instrument is queryable at once (CUC-08). An **internal**
   instrument arrives with its Policy/Standard/Control content imported as `draft`,
   owned by the restorer: chat says so plainly, and each Policy proceeds through
   CUC-10's propose and a different person's approval before it governs anything.

## Artifacts

- The catalog listing: a read-only report (when discovery happens here).
- One **run artifact** per restore: the terminal record of stages and statuses.
  Non-editable, non-scored.

## Approvals & notifications

- No passkey ceremony — restore is idempotent and additive-to-own-content; the gate is
  the chat-level confirmation.
- A completion notification only if the run outlives the conversation (with a blocking
  call this is the degenerate case; it becomes real if restore grows run tracking).
- No Inbox items.

## Contracts touched

- **H-1 Tool loop** — listing and restore execute as client tool calls.
- **H-4 Permissions & approvals** — the restate-and-acknowledge step before the effectful
  call.
- **PSC-6 Conversation persistence** — conversation, listing report, and run record
  restore together.
- **PSC-8 Run progress** — the run-record half; the live-progress half only applies once
  restore reports stages during execution rather than after.
- **PSC-10 Artifact type registry** — report and run flags drive the pane.

## Open points

- Whether restore grows ingestion-style run tracking (`run_id`, observable stages) or
  stays a blocking call the client merely renders a record of. A blocking multi-minute
  call sits awkwardly in a chat client; CUC-04's open point about the client-runtime
  watch loop applies here only in the first case.
- Whether the catalog listing deserves freshness handling (the source can change between
  listing and restore) or the named error states cover it acceptably.
