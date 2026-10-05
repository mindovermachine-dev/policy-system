---
name: ps-qna
description: Ask a compliance question and get an answer grounded in PS Service's live knowledge graph, with a source_ref citation and falsification-verified confidence.
---

# ps-qna

## Purpose

Turn an open/global question into a single, scoped, answerable question
grounded in the Policy System's actual entities — not the user's assumed
vocabulary — then run freehand retrieval, construct a first-pass answer,
and attempt to falsify it: step 1a (narrowing), steps 2-3 (retrieval,
construction), and step 5 (falsification) of the Guided Fitness Pipeline.
Rubric-gated fitness-function authoring (step 1b) and the independent
verification loop (step 4) are scoped out entirely — falsification is
the verification method.

**Deliverable:** a confirmed plain-language intent statement, then the
answer with inline `source_ref` citations and one confidence line
stating the graph was queried live and the falsification outcome. Full
detail (queries, rows, attempts, entities/edges, filters) is available on
request, not shown by default.

## On Load

1. If the user hasn't provided a question, ask for one before doing
   anything else.
2. Fetch the domain concepts by calling the `domain_concepts` MCP tool on
   the PS Service connector — the same text its `psdomain://concepts`
   resource serves, and the only source of truth for entities,
   relationships, and vocabulary. Call the tool, not the resource: some
   hosts expose MCP tools but not resources. `ps-mcp` is the only
   recognised connector name; one exposing `cypher` but no
   `domain_concepts` is not a PS Service connector either — report it as
   unreachable (error-state table, Process step 2; full test: see
   Guardrails). Always refetch this turn — never reuse a prior turn's or
   session's copy, and never assume the schema is unchanged. If the call
   fails, or returns a string beginning `error:`, report that distinctly
   (same table) before doing anything else — never fall back to
   memorized domain vocabulary.

## Core Principles

- Socratic method: one targeted question at a time; wait for the answer.
- Every move must tie back to a real entity/edge in the fetched domain
  concepts — never invent or assume vocabulary the model doesn't have.
- Adjust friction to the user: fewer questions when intent is clear, more
  when genuinely ambiguous. Resolve until answerable, not until trivial.
- Retrieval is genuinely freehand, grounded in the fetched domain
  concepts' actual property names and edge directions — never a template
  or an invented property/relationship.

## Process

1. **Intent gate.** State back, in plain language, what you understand
   the user wants to learn and the scope — including any default
   active-only bound (e.g. "counting only currently active
   requirements") — and ask them to confirm or correct it. The statement
   never uses a PascalCase schema label, an edge-type name, a property
   name, or Cypher. Before speaking, privately resolve the question
   against the fetched domain concepts: entity type(s), relationship(s)/
   traversal direction, the scope bound (one regulation vs. all; one
   capability vs. org-wide), and the counting unit if the question asks
   "how many" (distinct Controls vs. distinct chains yield different
   numbers).

   **Active-only default.** For any entity in the chain whose status enum
   literally includes an `active` value — currently RegulatoryInstrument
   (active\|superseded\|vacated), Requirement, PracticeArea, RiskPath
   (all active\|deprecated), Capability (active\|deprecated\|merged) —
   filter it on `status = 'active'`
   automatically, independently per entity (they can diverge, e.g. a
   Requirement deprecated under an active RegulatoryInstrument), and
   state each applied filter in the gate statement. A Capability with
   status `merged` is a tombstone — a duplicate a Compliance Officer
   absorbed into a surviving Capability — and is never a live Capability,
   so the default excludes it from every answer: its Obligations, coverage
   and policy are counted on the survivor. Include tombstones only when the
   user explicitly asks about merged duplicates or merge history, and say
   so in the gate statement. This does not extend
   to Policy (draft\|approved\|deprecated) or Standard/Control
   (`implementation_status`: planned\|draft\|implemented\|reviewed\|
   deprecated) — ask explicitly instead. An entity with no status
   property (Role, Obligation) is covered transitively; don't invent one.

   **Null-status fallback.** Before reporting a zero-row result produced
   by the active-only filter as "the graph does not contain this
   information," check whether the same query, with only that one
   entity's `status = 'active'` clause removed, returns rows whose
   `status` property is null for every one of them — the known
   legacy-ingestion gap (issue #109): nodes minted before status was set
   at ingest carry no `status` property at all. When it holds, relax that
   entity's filter, re-run without it, and disclose the relaxation
   explicitly — in the gate statement if discovered now, or in the Caveat
   line if discovered during retrieval — never silently. Do not relax the
   filter when some matching rows carry `status = 'active'` and others
   are null for the same entity type in the same query — a null row
   sitting alongside active ones is a genuinely mixed population, not the
   all-null legacy signature, so the strict filter stays in force.

   The gate may use the plain English words "policy," "standard,"
   "control," "capability," "obligation," "requirement," "role," and
   "category" when that is the natural word for the user's own concept —
   "category" stands in for the classification layer (PracticeArea,
   RiskPath) specifically. A word is forbidden schema vocabulary if and
   only if it appears in the fetched `domain_concepts` tool output as a
   literal node label, edge type, or property name — not based on how the
   sentence happens to capitalize it. The gate never uses a label, edge
   type, or property name that meets that test, and never uses Cypher.

   Also raise, in plain language, whichever of these apply — never
   default silently:
   - Policy/Standard/Control lifecycle bound not yet addressed → ask explicitly before confirming.
   - Bundled question (count + separate fact lookup) → ask split-vs-keep; state the choice in the gate.
   - Hypothetical subject/no existing anchor → ask if an attribute/category-filter answer is
     acceptable (check the classification layer first — see the "category" rule above).
   - Cross-layer comparison (Role/Standard/Control) → state the relocation to Obligation/Capability
     explicitly, not silently.

   On a correction, a scope change, or a question about how the question
   is structured, respond conversationally, one question at a time, and
   re-state the full intent statement — not just the delta. Run no
   retrieval during this loop. Once confirmed, retrieval starts
   immediately, with no further approval prompt.

2. **Retrieve and construct.** Immediately print: "Querying the knowledge
   graph and verifying the answer — this may take a moment." Print
   "Retrieving data from the graph…" and write ad hoc Cypher — genuinely
   freehand, never templated — via the `cypher` MCP tool on the connector
   selected at On Load, never a subprocess or spawned binary. More than
   one query is fine; no result goes unreported. A fuzzy-matched name
   resolving to more than one real node is never silently picked — report
   each connected candidate separately and flag it via the Caveat line.

   Every non-success result from the `cypher` tool call (this also covers
   the On Load `domain_concepts` call) must be distinguished into one of
   these named states — never collapsed into a generic "no answer":

   | Result shape                                                            | Named state                                                  | Notes                                                                                                                                                                                              |
   | ----------------------------------------------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure or auth-rejection                          | "PS Service is unreachable or the caller is unauthenticated" | Report distinctly and stop — do not construct an answer.                                                                                                                                           |
   | Rate-limit/throttle-shaped error (`429`-equivalent)                     | "PS Service is throttling requests"                          | PS Service does not yet enforce server-side rate limiting — not exercisable end-to-end today, but the skill must still recognize and report it distinctly. Never mistake for unreachable or empty. |
   | `error:` from a write/schema-change clause rejected by the Query Engine | "the query was rejected by the Query Engine"                 | Report the rejection verbatim where possible — never present as "no data," and stop.                                                                                                               |
   | Zero rows, or an explicit "graph is unseeded" signal                    | "the graph does not contain this information"                | State plainly; do not fabricate, round up, or fill the gap with assumed knowledge.                                                                                                                 |
   | Rows returned                                                           | —                                                            | Proceed below.                                                                                                                                                                                     |

   Print "Building the answer…" and build a plain-English answer from the
   retrieved rows — only what the data supports. Every factual claim must
   trace to a `source_ref` actually present on the rows/edges (e.g.
   "Art. 11(1)"); an unsupported claim is not made.

3. **Falsify.** Print "Verifying the answer…" and invoke
   `references/falsification-step.md` fresh, following its Process
   exactly, and fold its outcome into the confidence line below; the
   domain concepts fetched at On Load are still valid — do not refetch.
4. **Output.** Show only the answer plus the confidence line, unless a
   caveat applies: "<Answer prose, every claim carrying an inline
   source_ref, e.g. (Art. 11(1))>. Verified live — survived
   falsification." On a landed contradiction, state what it contradicts,
   without softening — e.g. "Not verified live — falsification found a
   Requirement in the same Article that narrows this duty to a smaller
   class of manufacturers."

   Insert a Caveat line when the null-status fallback relaxed a filter, a
   keyword/theme filter stood in for a modeled traversal, or a name
   resolved to more than one node with data — e.g. "Caveat: this count
   includes records with no active/inactive status on file, since
   excluding them would have hidden them entirely" — omitted otherwise.
   On a named error state, skip the block and report that state plainly.

   **On request** (queries, rows, falsification attempts, entities/edges,
   or filters), show exactly what was asked for, reconstructed only from
   work already done this exchange — never re-run, never fabricated.
   Filters example: "Requirement.status filter relaxed — status is
   unpopulated for all 12 matching Requirements (pre-issue-109 legacy
   data), not disabled by choice."

## Guardrails

- Never silently substitute a different entity/edge than the user's
  language implies, or let scope drift without re-confirming; never
  retrieve before intent is confirmed (step 1).
- Freehand, read-only retrieval only, via the `cypher` MCP tool on a
  connector whose `domain_concepts` tool answered at On Load (`ps-mcp`)
  — never `ps query template`/`ps query catalog`, a different connector
  name, or one offering `cypher` without `domain_concepts`. A rejected
  query (per the error-state table) stops the skill; never resubmitted
  as read-only.
- No rubric-gated fitness function or independent verification loop
  (Purpose) — falsification (step 3) alone verifies, runs at least once,
  reports plainly, survived or landed, without hedging; further attempts
  follow `references/falsification-step.md`'s cap.
- Don't silently collapse a compound question, force a same-node match
  across a non-convergent layer, or invent a status an entity has none
  of — surface each as an explicit choice.
- The active-only default (step 1) always appears in the gate statement,
  never extending to Policy/Standard/Control. The null-status fallback
  (step 1) is the only sanctioned relaxation, only when `status` is null
  for every row of that type, never on a bare zero-row result, and never
  undisclosed.
- Never collapse step 2's four error states into each other or into a
  generic "no answer"; the on-request detail handler reconstructs only
  from work already done this exchange, and status lines (step 2) name
  no entity, query, or attempt.
