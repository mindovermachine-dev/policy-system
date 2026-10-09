<!-- © 2026 Cartman ApS. All rights reserved. -->
# CUC-08: Ask a gap question

**Status:** Specified
**Actor:** Any authenticated user with graph read access; the reference walkthrough's
Compliance Officer asking "do we have a gap for DORA incident reporting?"
**Goal:** A grounded, citation-backed answer to a compliance question — rendered as a
read-only report the user can act on, including pivoting into authoring (CUC-09).
**Realizes:** UC-3
**Interaction class:** Q&A → report
**v1:** yes

A clickable mockup of this use case (together with CUC-09 and CUC-10) exists at
`../ps-client-mockup/index.html`.

## Entry points

- A new conversation: the user opens ps-client and asks.
- The Library's conversation list (CUC-11): resuming a persisted Q&A conversation, its
  report artifacts restored.

## Preconditions

- Caller is authenticated; the Q&A path is read-only, so no elevated role is required.
- The graph holds content worth asking about — instruments ingested or restored
  (CUC-04/CUC-05); without them the honest answer is "the graph does not contain this."

## Beats

1. **Ask.** The user poses the question in chat. The Artifact Pane is untouched; nothing
   renders until there is a grounded answer to render.
2. **Intent gate.** The AI states back, in plain language, what it understands the user
   wants to learn and the scope it will apply — including any defaults it is applying
   (e.g. counting only currently active requirements) — and asks the user to confirm or
   correct. One question at a time; genuinely ambiguous questions get more friction,
   clear ones less. The gate never leaks schema vocabulary or query syntax into chat.
3. **Live retrieval.** On confirmation the AI retrieves against the live graph through
   server tool calls, constructs the answer, and attempts to falsify it. Chat may carry a
   brief progress signal; raw queries and rows stay out of chat.
4. **Report.** The Artifact Pane renders a **read-only report** in a new tab — for the
   reference gap question: the Capability, its Obligations, the missing governing Policy,
   `source_ref` citations intact. Chat carries a short phrased answer plus one confidence
   line stating the graph was queried live and the falsification outcome, then asks what
   the user wants to do. Full detail (queries, rows, falsification attempts) is available
   on request, not shown by default.
5. **Follow-up.** Further questions in the same conversation refine or extend the answer;
   each structured answer renders as a report artifact, accumulating as tabs. Earlier
   reports stay one click away.
6. **Act or leave.** The user pivots to authoring — "help me close this gap" — entering
   CUC-09 with the report as its source context, or simply leaves. Either way the
   conversation persists first-class (unanchored unless authoring began), resumable with
   its reports (CUC-11).

## Artifacts

- One read-only report per structured answer, citations intact; typed as
  non-editable, non-scored under the artifact contract.

## Approvals & notifications

None. The path is read-only end to end: no passkey ceremony, no Inbox items, no
notifications.

## Contracts touched

- **H-1 Tool loop** — schema fetch, retrieval, and falsification all run as client-executed
  tool calls against PS Service; the model never touches the graph directly.
- **H-2 Streaming** — the phrased answer streams into chat; the report renders
  progressively as results land.
- **H-3 Context management** — the intent gate's confirmed scope and prior answers stay
  available across follow-ups.
- **H-6 Provider abstraction** — gap-question routing is the dominant task the §2
  local-model assumption is tested on (SP-2); this use case must not assume a hosted model.
- **PSC-6 Conversation persistence** — the unanchored Q&A conversation persists
  first-class; report artifacts restore on resume.
- **PSC-10 Artifact type registry** — the report's editable?/scored? flags (no/no) drive
  the pane's rendering.

## Open points

- Whether a report's structure comes from a served report template/type or is
  model-constructed per answer (the artifact contract supports both; D-3 left it open).
- How a restored report is reproduced on resume: persisted server-side as a materialized
  artifact, or re-derived — re-derivation risks silently differing from what the user saw.
