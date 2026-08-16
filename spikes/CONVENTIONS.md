<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike conventions

## Each spike is self-contained

A spike's README must be readable — and its code runnable — without an
agent or reader opening any other spike directory.

- **Needed files:** if a spike needs a file that exists in a previous
  spike (a schema, a comparison script, a requirements list), **copy it
  into the new spike's directory**. Do not have the README say "port
  from `spikes/x/y.py`" and leave the file absent — copy it in as the
  starting point, then adapt.
- **Needed ideas:** if a previous spike's *approach* is relevant but not
  literally reusable, say so as a plain design note in this spike's own
  voice ("this spike writes graph-to-graph directly, no JSON
  intermediate") — not as a narrated comparison ("spike X did Y, which
  didn't test the hypothesis, so this spike does Z instead").
- **No lineage sections.** Don't write "Relationship to spike X",
  "What this doesn't trust from spike X", or similar history/rationale
  sections. That reasoning belongs in commit messages or an optional
  `HISTORY.md` in the *earlier* spike, not carried forward into every
  descendant's default-loaded context.
- **Runtime dependencies are fine to name.** A spike consuming another
  spike's *output artifact* (e.g. a database populated by a prior spike)
  should say so plainly — that's current-state data flow, not history.

## Why

Each new "spikeN+1" was accumulating narrative explaining what
spikeN concluded and why, compounding across generations — an agent
working in spike 4 was pulling in the reasoning trail of spikes 2 and 3
just by reading its own README. Copying forward what's actually needed
and dropping the narrative keeps each spike's context bounded to itself.
