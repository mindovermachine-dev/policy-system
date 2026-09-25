---
name: ps-near-miss-review
description: List and resolve near-miss pending reviews (Company Merge dedup candidates) in the Policy System compliance graph — keep two entities separate or merge them, with the merge effect made explicit before it runs.
---

# ps-near-miss-review

## Purpose

List every unresolved near-miss `PendingReview` — a Company Merge dedup
candidate awaiting a human decision — and resolve one by keeping its two
entities separate or merging them. This replaces ps-cli's `near-misses list`/
`near-misses resolve` commands (issue #126) with MCP-tool-backed equivalents
reachable by any MCP-capable client, with no locally-installed, signed
ps-cli binary required.

**Scope covered so far:** listing unresolved reviews (`near_misses_list`)
and resolving one with either `decision="keep-separate"` or
`decision="merge"` (`near_misses_resolve`). The tool itself now enforces a
real confirmation step before a merge ever executes (see Core Principles
and Process below) — this skill's own job is to make sure the user has
actually seen and understood the irreversibility warning before that
confirmation is answered, not to treat the tool's gate as a substitute for
that.

**Deliverable:** every unresolved review's `id`, `kind`, `incoming_text`,
`nearest_existing_text`, and `similarity` on listing; the resolved
review's `review_id` and `decision` (plus `winner_id`/`loser_id` —
populated for `merge`, `None` for `keep-separate`) on resolving — or the
specific named error state on failure.

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
under the right name but does not expose both a `near_misses_list` tool
and a `near_misses_resolve` tool is **not** a PS Service connector either,
whatever it is named — report it as unreachable (see the error-state
table under Process) rather than proceeding against it. From here on,
"the PS Service connector" means the one selected here.

## Core Principles

- Never fabricate a review or a field value — report exactly what the tool
  returned, for every unresolved review, not just a subset.
- Never collapse the five reported fields (`id`, `kind`, `incoming_text`,
  `nearest_existing_text`, `similarity`) into a shortened summary — a
  reviewer needs all five to judge the pair.
- Never silently retry a failed call — report the named failure and stop.
- Never resolve a review without the user having named which review
  (`id`) and which decision they want — never guess or default either.
- `decision="merge"` is IRREVERSIBLE — it deletes the loser node and
  re-points every edge that referenced it onto the winner, atomically,
  before the call returns. State this out loud to the user, in these terms,
  before ever proposing that path — not only when the tool itself is
  called, and not as a substitute for the tool's own confirmation step
  below (belt-and-braces: this skill's own warning and the tool's own gate
  are two independent safeguards, not one relied on to cover the other).
- Calling `near_misses_resolve` with `decision="merge"` does not execute a
  merge immediately: the tool pauses and asks the connected client to
  confirm before it runs (an MCP elicitation request carrying the same
  irreversibility warning). Never treat that pause as a failure or an
  unexpected error — it is the tool's own confirmation gate, and answering
  it (or declining it) is what determines whether the merge proceeds.
  Never auto-accept it on the user's behalf without having already told
  them, in this conversation, exactly what the merge will do.
- `decision="keep-separate"` only deletes the pending review record
  itself — it never touches the two entities the review compared, and it
  never triggers the confirmation pause above (that gate applies to
  `merge` only).

## Process

### Listing reviews

1. **Call the tool.** Invoke `near_misses_list` (no arguments) on the PS
   Service connector selected at On Load. This is a read-only call.
2. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "listing failed":

   | Tool result shape                                               | Named state to report                                                                                                                                                                             |
   | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error | "PS Service is unreachable or the caller is unauthenticated"                                                                                                                                      |
   | `error: the policy graph database is not reachable`             | The compliance graph database cannot be reached — report it distinctly from a PS Service transport failure                                                                                        |
   | `error: an unexpected error occurred`                           | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                                                                 |
   | Successful structured response                                  | Report every unresolved review's `id`, `kind`, `incoming_text`, `nearest_existing_text`, and `similarity` plainly, including an explicit "no unresolved reviews" statement when the list is empty |

3. **Output**, in this shape on success:

   ```text
   Unresolved near-misses: <count>

     <id> [<kind>] similarity=<similarity>
       incoming: <incoming_text>
       existing: <nearest_existing_text>
     ...
   ```

   On a named error state, report that state plainly instead — do not emit
   an Output block that implies a successful listing when none occurred.

### Resolving a review with `keep-separate`

1. **Confirm the inputs with the user** before calling anything: which
   review `id` (normally one already surfaced by a prior listing step —
   list first if the user hasn't named one and doesn't already know it),
   and that they want `decision="keep-separate"`. Never infer the `id` or
   default the decision.
2. **Call the tool.** Invoke `near_misses_resolve` with `review_id` and
   `decision="keep-separate"` on the same PS Service connector.
3. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "resolve failed":

   | Tool result shape                                               | Named state to report                                                                                                                                                                                                |
   | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error | "PS Service is unreachable or the caller is unauthenticated"                                                                                                                                                         |
   | `error: no unresolved PendingReview with id '<review_id>'`      | "Review not found or already resolved" — the review either never existed or was already resolved (these two conditions are indistinguishable server-side, by design; report them as one state, not a guess at which) |
   | `error: the policy graph database is not reachable`             | The compliance graph database cannot be reached — report it distinctly from a PS Service transport failure                                                                                                           |
   | `error: an unexpected error occurred`                           | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                                                                                    |
   | Successful structured response                                  | Report the resolved review's `review_id` and `decision` plainly; state explicitly that `keep-separate` only removed the pending review record and left both compared entities untouched                              |

### Resolving a review with `merge`

1. **Surface the irreversibility warning first, in plain language, before
   proposing or confirming this path with the user:** merging deletes the
   loser node and re-points every edge that referenced it onto the winner,
   atomically, and this cannot be undone. Do this even if the user already
   said "merge" — the point is that they have actually seen the effect
   stated, not merely typed the word.
2. **Confirm the inputs with the user** before calling anything: which
   review `id` (normally one already surfaced by a prior listing step —
   list first if the user hasn't named one and doesn't already know it),
   and that they want `decision="merge"` specifically, having understood
   step 1's warning. Never infer the `id` or default the decision.
3. **Call the tool.** Invoke `near_misses_resolve` with `review_id` and
   `decision="merge"` on the same PS Service connector.
4. **Expect and handle the tool's own confirmation pause.** The call does
   not execute the merge immediately — it elicits a confirmation from the
   connected client, carrying the same irreversibility warning. This is
   expected behavior, not an error: complete whatever confirmation flow
   the client surfaces (e.g. relaying the warning to the user again and
   answering on their explicit go-ahead), never auto-confirming without a
   clear, current "yes" from the user for this specific review.
5. **Report the result**, distinguishing every non-success outcome into one
   of the following named states — never collapsed into a generic
   "resolve failed":

   | Tool result shape                                                                                                                           | Named state to report                                                                                                                                                                                                                                 |
   | ------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error                                                                             | "PS Service is unreachable or the caller is unauthenticated"                                                                                                                                                                                          |
   | The confirmation is declined or cancelled                                                                                                   | "Merge not confirmed — no change was made"; the loser and winner nodes are both untouched                                                                                                                                                             |
   | `error: no unresolved PendingReview with id '<review_id>'`                                                                                  | "Review not found or already resolved" — the review either never existed or was already resolved (these two conditions are indistinguishable server-side, by design; report them as one state, not a guess at which)                                  |
   | `error: pending review '<review_id>' references a node that no longer exists (already resolved by a prior merge); this review is now stale` | "Review references an already-merged node (stale)" — report this distinctly from "not found": the review itself exists, but the entity it compared no longer does, because a different merge already resolved it first; no write happened here either |
   | `error: the policy graph database is not reachable`                                                                                         | The compliance graph database cannot be reached — report it distinctly from a PS Service transport failure                                                                                                                                            |
   | `error: an unexpected error occurred`                                                                                                       | An unrecognised failure — report it as an unexpected error, distinct from every other named state above; never guess at its cause                                                                                                                     |
   | Successful structured response                                                                                                              | Report the resolved review's `review_id`, `decision`, `winner_id`, and `loser_id` plainly; state explicitly that the loser node was deleted and its edges re-pointed onto the winner, atomically                                                      |

## Guardrails

- The skill reaches PS Service exclusively through a recognised MCP
  connector — `policy-system-graph` or `policy-system-graph-local` — never
  a direct graph connection, a repo-local script, or a spawned external
  binary.
- Never collapse the named error states in the Process tables into each
  other or into a generic failure message.
- Never fabricate a review, a field value, or a count that the tool did not
  actually return.
- Never omit a returned error — surface it plainly rather than treating a
  failed call as an empty list.
- A merge is IRREVERSIBLE: it deletes the loser node and re-points every
  edge that referenced it onto the winner, atomically, before the call
  returns. State this to the user in your own words before proposing or
  confirming `decision="merge"` — belt-and-braces alongside the tool's own
  confirmation gate, never a replacement for it, and never skipped because
  the tool will "ask anyway."
