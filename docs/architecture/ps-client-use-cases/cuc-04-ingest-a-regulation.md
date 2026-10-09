<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-04: Ingest a regulation

**Status:** Specified
**Actor:** Compliance Officer (the `start_ingestion`/`get_ingestion_status` pair is
gated on the `ComplianceOfficer` grant)
**Goal:** One EU regulation, identified by CELEX, ingested through the full pipeline into
the merged company graph — with the run observable live, resumable after leaving, and
persisted as its own record.
**Realizes:** UC-1
**Interaction class:** Long-running run
**v1:** yes

## Entry points

- A new conversation: "ingest the CRA."
- The Library (CUC-11): reopening the conversation of a run that is still going — or its
  run artifact — to see where it stands.
- A completion notification: "the run you were watching finished" leads back to the same
  conversation and artifact.

## Preconditions

- Caller is authenticated and holds the `ComplianceOfficer` grant.
- The CELEX exists on Cellar/ELI. A CELEX already ingested is rejected, not re-run (a
  legacy node predating CELEX tracking yields an explicit already-ingested outcome
  instead; either way nothing new runs).
- The user supplies the instrument's `short_name` — it is never derived or guessed.

## Beats

1. **Ask.** The user names the regulation. The AI resolves what it can but asks for
   whichever of the two inputs — CELEX identifier and intended `short_name` — the user has
   not given. No artifact yet.
2. **Confirm.** Submission writes to the compliance graph, so both inputs are restated
   back and acknowledged before anything is submitted — an H-4 acknowledgment in chat, not
   a passkey ceremony (ingestion is add/merge-only, no lifecycle boundary).
3. **Submit.** The run is accepted and returns its `run_id` immediately; an audit row
   records the submission before the pipeline runs — if it cannot, the run does not start
   (fail closed). A **run artifact** opens as a new tab: the four stages — ingestion,
   extraction, derivation, merge — laid out with the current stage live.
4. **Watch — or don't.** Stage transitions update the artifact as they happen; chat
   narrates on stage change, never per status check. A real run takes minutes, so leaving
   is normal: the run continues on PS Service regardless, and the conversation and
   artifact persist. A slow run is not a failed run; nothing is ever resubmitted or
   silently retried.
5. **Return.** Through any entry point the user lands on the same conversation, the run
   artifact showing current state. On completion a notification fires — "something you
   were watching finished" — never an Inbox item for the submitter.
6. **Terminal state.** On success the artifact becomes the run's persistent record: the
   resolved instrument id and the per-stage summary, including the merge counts (new
   Obligations, new Capabilities, matched Capabilities); chat phrases the short version.
   On failure the artifact and chat carry the specific named error state — visibly,
   per the fail-closed posture.
7. **Consequences.** The merged graph is immediately queryable (CUC-08). If Company Merge
   left near-miss candidates, those are decisions, not news — they surface as
   system-generated Inbox items (CUC-06), not as part of this run's notification.

## Artifacts

- One **run artifact**: live per-stage progress while running, then the run's permanent
  record (summary, counts, or named failure). Non-editable, non-scored.

## Approvals & notifications

- No passkey ceremony — ingestion is additive; the only gate is the chat-level
  confirmation of both inputs before submission.
- One completion notification to the submitter (success or failure alike).
- Indirectly: near-miss candidates minted by the merge become system-generated Inbox
  items — CUC-06's entry, governed by Q-12.

## Contracts touched

- **H-1 Tool loop** — submission and status checks execute as client tool calls.
- **H-2 Streaming** — progressive run-artifact updates as stages transition.
- **H-4 Permissions & approvals** — the restate-and-acknowledge step before an effectful
  submission.
- **PSC-6 Conversation persistence** — the submitting conversation and its run artifact
  survive closing the client mid-run.
- **PSC-7 Notifications & Inbox** — the completion notification; the near-miss Inbox
  items on the consequence path.
- **PSC-8 Run progress** — per-stage status for a submitted run, observable after the
  submitting conversation closes; this use case is PSC-8's reason to exist.
- **PSC-10 Artifact type registry** — the run type's flags (not editable, not scored)
  drive the pane.

## Open points

- How progress reaches the artifact: today's skill polls on a budget inside the model's
  tool loop; in ps-client the watch loop plausibly belongs to the client runtime
  (subscribe or poll out-of-band) so a long run never consumes model context. PSC-8's
  capability-register row ("subscribe/notify path") covers the server half; the client
  half is a TD-1/TD-5 concern.
- Whether run records appear in the Library's document list alongside reports and drafts,
  or only inside their conversations.
- Notification delivery when no client is open (desktop closed, web tab gone) — scope of
  PSC-7 that no decision covers yet.
