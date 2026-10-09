<!-- © 2026 Cartman ApS. All rights reserved. -->
# ps-client UX mockup

A clickable, single-file HTML mockup of ps-client's two-pane UX, playing the
gap-to-closed journey (CUC-08 → CUC-09 → CUC-10, see `../ps-client-use-cases/`)
per the design in `../ps-client-ux-design.md` §3.

## Run

Open `index.html` in any browser. No server, no build, no dependencies.

## What it demonstrates

- **Two panes:** chat left; a structured-artifact viewport right, with tabs
  (gap report, Policy draft, Standard draft) that stay one click away.
- **Scripted session:** the chat input is pre-filled with the next scripted
  user message — press Send (or Enter) to advance beat by beat. The document
  scaffolds immediately, then fills and re-scores section by section, top to
  bottom.
- **Read-only report vs editable draft:** the gap report renders as a
  citation-bearing report; the Policy draft is the editable, rubric-scored
  artifact.
- **Edits implicit, progression explicit:** sections update live as the
  conversation converges; when one passes, the AI says so and asks to move on.
- **Direct manual editing:** at the Scope beat, type into the Scope section
  itself and click elsewhere — the AI detects the change, re-scores, comments,
  and returns to where it left off.
- **Badge drill-down:** click any section's score badge for the per-criterion
  pass/fail table with reasons.
- **Deliberate lifecycle event:** when the tree passes, the Propose control on
  the document opens a signing summary (tree, scores, digest) and a simulated
  passkey ceremony; status flips draft → proposed.

## What is faked

Everything. There is no model, no PS Service, no rubric engine, no WebAuthn —
all content, scores, criteria IDs and the digest are hard-coded to make the
interaction model tangible. Standards/Controls are represented by one
pre-scored Standard tab rather than a full authoring loop.
