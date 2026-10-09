<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-09: Author a policy tree (gap-to-closed)

**Status:** Specified
**Actor:** Compliance Officer or Policy Manager
**Goal:** A Policy with its Standard(s) and Control(s), every document's rubric score
clearing its `pass_threshold`, left draft and ready to propose (CUC-10).
**Realizes:** UC-2
**Interaction class:** Authoring
**v1:** yes

A clickable mockup of this use case (together with CUC-08 and CUC-10) exists at
`../ps-client-mockup/index.html`.

## Entry points

- A gap-report conversation (CUC-08): "help me close this gap."
- An Inbox assignment (CUC-12): authoring work assigned for an ingested instrument.
- The Library (CUC-11): resuming this document's own thread, from either the conversation
  or the document side.

## Preconditions

- Caller is authenticated and holds a role permitted to author (PolicyManager or above for
  lifecycle ownership; see `ps-author-policy`'s access behavior).
- A named Capability exists in the graph. If it is already governed, the branch rules
  apply: resume the caller's own draft, or fork a superseding draft from a
  proposed/approved Policy — never a competing Policy.

## Beats

1. **Scaffold.** The user asks to close the gap. The client creates the draft on the
   server; a new tab opens in the Artifact Pane showing the full document scaffold —
   every section visible, pending markers where content is missing — already linked to
   the Capability, with the uncovered Obligations carried in as its scope. The source gap
   report (when entered from CUC-08) stays one tab away.
2. **Section loop, top to bottom.** Per section: the AI asks one Socratic question against
   the weakest rubric criterion; the user answers; the section updates live in the
   document; the section's score badge updates. When a section passes, the AI says so
   plainly and asks whether to move on — the user controls the pace. A section stuck
   after 2–3 passes gets a passing example shown.
3. **Manual edit.** At any point the user types directly into a section. The client
   persists the edit through the same draft contract, the AI states that it detected the
   change, re-scores, comments on the effect (including contradictions with earlier
   sections) — then follows the user's focus and returns to its bookmarked place in the
   top-to-bottom flow when that thread is done. The user's typing always wins a collision.
4. **Badge drill-down.** Clicking any section's score badge renders that section's rubric
   as a table — each criterion, pass/partial/fail, and a one-line reason — so the
   document explains itself without the chat.
5. **Descend the tree.** The same loop produces Standard(s) and Control(s), each with its
   own rubric, its own badges, its own pass threshold.
6. **End state.** All documents in the tree pass; the AI confirms the holistic rescore and
   notes the tree is ready to propose. Proposing is CUC-10 — this use case never triggers
   a lifecycle transition. The user can leave at any beat and return (CUC-11).

## Artifacts

- The Policy draft and its Standard/Control drafts: editable, rubric-scored documents,
  one tab each (or one tree-navigable tab — presentation detail left open).
- The originating gap report (read-only), when entered from CUC-08.

## Approvals & notifications

None. Draft-mode edits never trigger a passkey ceremony; no Inbox items or notifications
are minted. The passkey boundary is CUC-10's.

## Contracts touched

- **PSC-1 Document read** — render every section's content and the tree on open/resume.
- **PSC-2 Document write** — field-level PATCH per answered section, from chat and from
  manual edits alike.
- **PSC-3 Rubric-as-data** — section structure, criterion ids/weights/guidance, threshold,
  per document type, without client changes for new types.
- **PSC-4 Scoring** — per-criterion results with reasons after every edit; the badge and
  drill-down render these.
- **PSC-5 Document sync** — a manual edit reaches the model and the pane; a chat-driven
  edit reaches the pane, live.
- **PSC-6 Conversation persistence** — this document's single accumulating thread.
- **H-1..H-3, H-6** — tool loop, streaming, context management, provider abstraction.

## Open points

- Whether the Standard/Control drafts render as separate tabs or one tree-navigable
  document view.
- Scoring location (server component vs client model with persisted scorecards) — carried
  in ps-client-ux-design.md §8; this use case only requires PSC-4's result shape.
