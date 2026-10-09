<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-11: Return & resume

**Status:** Specified
**Actor:** Any authenticated user returning to earlier work — same device or another
**Goal:** Land back in a prior discussion exactly once through any door: the thread
continued, its artifacts restored to *current* state, scores intact, with a truthful
recap of where things stand.
**Realizes:** —
**Interaction class:** —
**v1:** yes

Every other CUC names this one for its "leave and come back" behavior; this document is
that behavior, once.

## Entry points

This use case *is* the entry points. All of them converge on the same restored state —
the discussion with its artifacts — and none creates a second copy:

- The **Library's conversation list** — pure Q&A conversations included, first-class.
- The **Library's document list** — entering from the document side lands in that
  document's one thread.
- An **Inbox item** (CUC-12) — the assignment opens the relevant discussion and artifact.
- A **notification** — "the run you were watching finished" (CUC-04) reopens the
  watching conversation.

## Preconditions

- The conversation or document exists server-side; the caller is authenticated (CUC-01)
  and entitled to it. Days or weeks may have passed; the device may be a different one.

## Beats

1. **Pick a door.** Whichever list or item the user enters through, the destination is
   identical: the discussion restored with its artifact tabs. Two doors to one thing,
   never two things.
2. **Restore to current, not to a snapshot.** Tabs rebuild from server state at their
   *present* values: a draft shows its current content and scores — including edits made
   from another session since the user left — a run shows its current stage or terminal
   record, a proposed document shows its post-ceremony read-only state, a pending
   approval shows pending or expired as it now stands. What cannot be fetched greys out
   rather than rendering stale as current.
3. **Recap.** Chat recaps where things stood and what has changed since — a section that
   passed, a run that finished, an approval that expired. The recap is grounded in the
   thread and the restored artifact state, not remembered.
4. **Continue the thread.** A document's conversation is **one continuous thread** — part
   of the draft's governance record. Resuming continues it; no entry point forks a
   parallel history. Unanchored Q&A conversations continue the same way, their report
   artifacts restored.
5. **Pick up mid-flight work.** The stopped place is recoverable in every flow: the
   authoring loop resumes at its bookmark (CUC-09), a pending passkey approval can be
   checked and, if expired, restarted from preview (CUC-06/07), a finished run's record
   is simply there (CUC-04/05).

## Artifacts

Whatever the resumed conversation accumulated — restored per their registered types.
This use case adds no artifact class of its own.

## Approvals & notifications

None of its own. Resumption never re-triggers a ceremony; an expired approval is
reported, not silently renewed.

## Contracts touched

- **H-5 Session persistence** — the harness half: rebuild a conversation with its tabs
  from server state; this use case is H-5's definition.
- **PSC-6 Conversation persistence** — the server half: threads, session ↔ artifact
  links, one thread per document, unanchored conversations.
- **PSC-1 / PSC-4** — drafts re-read with full content and persisted scorecards; "scores
  intact" depends on scorecards being server-held, not re-derived on open.
- **PSC-5 Document sync** — restore-to-current on open, and live updates thereafter.
- **PSC-8 Run progress** — run state readable after the submitting conversation closed.
- **H-3 Context management** — the model's working context is reconstructed from a
  thread that may be weeks long; the recap must survive that reconstruction.
- **NFR-1** — identical resumption on desktop and web.

## Open points

- **Q-10** — a colleague with access opens a draft that has a thread: join it,
  read-only, or something else; multi-user thread semantics are undesigned, and "one
  thread per document" makes the answer consequential.
- **Q-11** — whether a thread freezes when its document leaves draft, and what
  retention/compaction applies to a governance-record conversation (CUC-02's
  invite-URL-in-transcript is the first concrete instance arguing for redaction rules).
- **Recap fidelity on long threads**: what the model is actually given on resume — full
  transcript, server-side summary, or compaction — is a TD-1 harness concern; the
  contract here is only that the recap never asserts state the artifacts contradict.
- Library presentation (ordering, search, whether run records list there — CUC-04's open
  point) is undesigned; the lists are load-bearing doors and will not stay usable at
  hundreds of conversations without it.
