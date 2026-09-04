---
name: ps-qna
description: Ask a compliance question and get an answer grounded in PS Service's live knowledge graph, with a source_ref citation and falsification-verified confidence.
---

# ps-qna

## Purpose

Turn an open/global question into a single, scoped, answerable question,
grounded in the Policy System's actual entities and relationships — not the
user's assumed vocabulary — then run freehand retrieval against the graph
via PS Service's MCP connector, construct a first-pass answer, and attempt
to falsify it. This covers step 1a (narrowing) plus steps 2-3 (freehand
retrieval, answer construction) and step 5 (falsification) of the Guided
Fitness Pipeline (`spikes/pipeline2/README.md`), the latter per
`spikes/pipeline3/README.md`. Rubric-gated fitness-function authoring
(step 1b) and the independent verification loop (step 4) are scoped out of
this skill entirely — falsification (adversarial querying against the
graph's own data) is this skill's verification method.

**Deliverable:** the refined question text (approved verbatim by the
user), the entities/edges it routes through, the freehand Cypher query
used to retrieve data, the retrieved data, a constructed first-pass answer,
and a falsification report (attempts made, landed or missed, per
`references/falsification-step.md`).

## On Load

1. If the user hasn't provided a question, ask for one before doing
   anything else.
2. Fetch the `psdomain://concepts` MCP resource via the
   `policy-system-graph` connector — this is the only source of truth for
   entities, relationships, and vocabulary this refinement grounds
   against. **Always refetch it now, this session, this turn** — never
   rely on a copy remembered from a prior turn or a prior session, and
   never assume the schema is unchanged since last time. If the resource
   fetch itself fails, report that distinctly (see the error-state table
   under Process step 5) before attempting anything else — do not fall
   back to memorized domain vocabulary.

## Core Principles

- Socratic method: one targeted question at a time; wait for the answer.
- Every narrowing move must tie back to a real entity/edge in the fetched
  `psdomain://concepts` resource — never invent or assume vocabulary the
  model doesn't have.
- Adjust friction to the user: fewer questions when intent is already
  clear, more when the question is genuinely ambiguous.
- Narrow until answerable, not until trivial — stop as soon as the mapping
  to entities/edges is unambiguous.
- Retrieval is genuinely freehand: write ad hoc Cypher grounded in the
  fetched resource's actual property names and edge directions — never
  route through `ps query template`/`ps query catalog`, and never invent a
  property or relationship the model doesn't have (mirrors README D10).

## Process

1. **Restate intent.** In one sentence, state back what you understand the
   user is actually trying to learn. Confirm or correct before proceeding.
2. **Socratic narrowing loop.** Ask one question at a time to resolve, only
   where still ambiguous:
   - Which entity type(s) the question is really about (RegulatoryInstrument, Role,
     Requirement, Obligation, Capability, Policy, Standard, Control,
     PracticeArea, RiskPath).
   - Which relationship(s)/traversal direction the question implies.
   - Scope bound (one regulation vs. all; one capability vs. org-wide).
   - Counting unit, if the question asks "how many" (e.g. distinct Controls
     vs. distinct chains — these yield different numbers over the same
     graph).
   - Status/lifecycle bound: default to active-only, applied automatically
     without asking, for any entity in the chain whose status enum
     literally includes an `active` value — currently `RegulatoryInstrument`
     (`active`\|`superseded`\|`vacated`), `Requirement`, `PracticeArea`,
     `RiskPath`, `Capability` (all `active`\|`deprecated`). Filter each
     such entity in the chain independently on `status = 'active'` — they
     can diverge (e.g. a Requirement individually deprecated under an
     active RegulatoryInstrument), so filter every one that has the property, not
     just one representative. Always state each applied filter explicitly
     in the proposed question (step 3) so the user can catch and override
     it — e.g. "including superseded/deprecated" opts out for a given
     entity.
     This default does **not** extend to `Policy`
     (`draft`\|`approved`\|`deprecated`) or `Standard`/`Control`
     (`implementation_status`: `planned`\|`draft`\|`implemented`\|
     `reviewed`\|`deprecated`) — neither has a literal `active` value, so
     still ask the user as before whether a lifecycle bound on these
     matters to the question.
     If an entity in the chain carries no status/lifecycle property at all
     (e.g. `Role`, `Obligation`), don't invent one — it's covered
     transitively by whichever adjacent entity's active-only filter
     already applies.
   - Whether the question bundles more than one distinct ask (e.g. a count
     plus a separate specific-fact lookup) — if so, ask the user whether to
     keep it as one approved question with explicit clauses, or split into
     separate step 1-4 passes. Don't default to either silently.
   - Whether the question's subject already exists as a node in the graph,
     or is hypothetical/forward-looking (e.g. a system not yet built). If
     hypothetical, say so explicitly and confirm whether an attribute/
     theme-filter answer (no traversal) is acceptable, rather than forcing
     a fake edge to something that doesn't exist yet. Before concluding
     there's no anchor at all, check whether the classification layer
     (PracticeArea, RiskPath — see the fetched resource's Document
     Purpose section) has a real matching category (e.g. RiskPath.
     risk_type) that could anchor the question instead of a bare
     keyword filter on Capability/Obligation text.
   - Whether the question implies comparing or matching entities across a
     layer the model deliberately keeps non-convergent (Role, Standard,
     Control — check the concept's own Identity note in the fetched
     resource). If so, confirm with the user whether relocating the
     comparison to the nearest convergent layer (typically Obligation or
     Capability) satisfies their intent — state explicitly that the
     comparison is being relocated, not answered directly at the layer
     they named.
3. **Propose the refined question** in plain English, together with the
   entities/edges it maps to. If step 2 surfaced a compound question the
   user chose to keep bundled, state each clause explicitly in the
   proposal rather than collapsing them into one sentence.

   **Pre-flight gate, mandatory, not discretionary:** if the proposal
   applies the default active-only filter to any entity, list each
   filtered entity explicitly in the proposed question text — never apply
   it silently/invisibly, even though it wasn't asked about. For `Policy`
   and `Standard`/`Control`, confirm a lifecycle bound was explicitly
   asked about in step 2 (per the bullet above) rather than defaulted or
   omitted. This is a checklist item to verify, not a judgment call about
   whether the ambiguity "feels" significant enough to raise.

4. **Approval gate.** Ask the user to approve the question verbatim. If not
   approved, continue the loop on the specific part that's wrong — don't
   restart from scratch. Only continue to step 5 once approved.
5. **Freehand retrieval.** Write ad hoc Cypher against the graph to answer
   the approved question — genuinely freehand, not assembled from a
   template library. Execute it by calling the plugin's own `cypher` MCP
   tool on the `policy-system-graph` connector — **never** a subprocess,
   a repo-local script, or any other spawned external binary. Show the
   query before or alongside its results — never hide what was actually
   run.

   More than one query is expected and fine — a discovery query to resolve
   an ambiguous entity name, or a follow-up aggregate query to compute
   exact statistics rather than hand-summing a large result set, are both
   legitimate freehand retrieval, not a violation of "the approved
   question." Show every query run, not just the last one.

   If a fuzzy/pattern-matched entity name resolves to more than one real
   node at retrieval time, don't silently pick one. If only one candidate
   actually connects to relevant data, that's an empirical resolution —
   say so explicitly in the answer. If more than one candidate connects
   with real data, report each candidate's results separately rather than
   merging them, and flag the ambiguity in the constructed answer — the
   approval gate already passed, so this can't be sent back to the user;
   the answer itself has to carry the caveat.

   **Every non-success result from the `cypher` tool call must be
   distinguished into one of the following named states — never collapsed
   into a generic "no answer" or "no data":**

   | MCP tool result shape                                                                   | Named state to report                                        | Notes                                                                                                                                                                                                                                                                                                                     |
   | --------------------------------------------------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | Connection/transport failure, or an auth-rejection-shaped error                         | "PS Service is unreachable or the caller is unauthenticated" | Report this distinctly and stop — do not proceed to answer construction.                                                                                                                                                                                                                                                  |
   | A rate-limit/throttle-shaped error (`429`-equivalent)                                   | "PS Service is throttling requests"                          | PS Service does not yet enforce server-side rate limiting (issue #39 Group 3, deferred) — this state is not exercisable end-to-end today, but the skill must be ready to recognize and report it distinctly the moment the server starts emitting one. Never mistake this for an unreachable endpoint or an empty answer. |
   | `error:` returned because a write/schema-change clause was rejected by the Query Engine | "the query was rejected by the Query Engine"                 | Report the rejection itself, verbatim where possible — never present a rejected query as "no data."                                                                                                                                                                                                                       |
   | Successful execution with zero rows, or an explicit "graph is unseeded" signal          | "the graph does not contain this information"                | State plainly that the information is not present — do not fabricate, round up, extrapolate, or fill the gap with assumed domain knowledge.                                                                                                                                                                               |
   | Successful execution, rows returned                                                     | —                                                            | Proceed to step 6 (construct the answer).                                                                                                                                                                                                                                                                                 |

   This same distinction applies to the On Load resource fetch (step 2 of
   On Load, above) — an unreachable/unauthenticated or throttled resource
   fetch is reported the same way, before any narrowing begins.

6. **Construct the answer.** Build a plain-English answer directly from the
   retrieved rows. State only what the data supports; do not round up,
   extrapolate, or fill gaps with assumed domain knowledge. Every factual
   claim in the answer must be traceable to a specific `source_ref` value
   actually present on the retrieved rows/edges (e.g. "Art. 11(1)", "§ 32
   Abs. 1 BSIG") — never cite "the graph" generically. If no retrieved
   row/edge carries a `source_ref` relevant to a claim, that claim cannot
   be made — say so rather than asserting it uncited.
7. **Falsify.** Invoke `references/falsification-step.md` against the
   constructed answer — read it fresh, follow its Process exactly
   (including its scope-aware attempt-cap determination — 1 attempt if the
   question's Entities stay within the ingested spine, 5 if `Policy`,
   `Standard`, or `Control` are involved, or the user asked for deeper
   scrutiny), and fold its Output shape into step 8 below. Falsification's
   result (landed vs. none landed) is this skill's verification signal for
   the answer. The `psdomain://concepts` resource fetched during On Load
   is still valid for this step — do not refetch it.
8. **Output**, in this shape:

   ```text
   Question: <refined question text>

   Answer: <constructed answer, every factual claim tied to a source_ref
   from the retrieved rows>

   Live: queried PS Service's MCP connector in this exchange; no vendored
   or cached copy of the graph or domain-concepts content was used —
   the psdomain://concepts resource and all retrieval results above were
   fetched fresh this session.

   Status: <Verified — survived falsification | Falsification landed a
   contradiction>. Falsification ran under a max_falsification_attempts
   cap of <1 | 5>, set per spikes/pipeline3/PROGRESS.md D6 because <the
   question's Entities stay within the ingested spine | Policy/Standard/
   Control is involved | the user asked for deeper scrutiny> — state which
   reason applied, don't just report the number. Falsification is this
   skill's verification method; no separate fitness-function check exists
   or is planned. If the answer was produced via an attribute/theme filter
   rather than a modeled traversal (Edges line's second or third case), add
   a second sentence naming that the result is also sensitive to the
   specific keyword/theme terms chosen — a different keyword list could
   return a different result set, regardless of falsification outcome.

   Falsification: <N> attempt(s), <landed | none landed>
     1. <query intent, one line> — <landed: what it contradicts | missed>
     2. ...

   Entities: <Label, Label, ...>
   Edges: one of —
     - <EDGE_TYPE, EDGE_TYPE, ...> — a real traversal, no ambiguity.
     - "none — attribute/theme filter, not a traversal" plus a one-line
       reason, if the question has no existing graph anchor at all.
     - <EDGE_TYPE, EDGE_TYPE, ...> plus a one-line note naming what's
       applied as an unmodeled keyword/theme filter on top, if the
       traversal is real but part of the question's filter (e.g. a theme
       like "PII" with no dedicated node/property) isn't itself modeled.
       Don't overstate the filter as a traversal step, and don't
       understate a real traversal as "none" just because one filter
       term isn't modeled.

   Filters: <status filters auto-applied by the active-only default, e.g.
     "RegulatoryInstrument.status = active, Requirement.status = active">, or "none"
     if no entity in the chain qualified for the default.

   Query:
   <the executed Cypher>

   Retrieved data:
   <rows, or a compact summary if large>

   ```

   If retrieval instead landed on one of the named error states from step
   5's table, skip the sections above that depend on retrieved data and
   report the named state plainly instead — do not emit an Output block
   that implies a successful query when none occurred.

## Guardrails

- Never silently substitute a different entity/edge than what the user's
  language implies — ask, don't assume.
- Don't let narrowing drift the question away from the user's original
  intent — if scope has shifted substantially since step 1, say so and
  re-confirm intent before continuing.
- Never execute retrieval before the user has approved the refined question
  verbatim (step 4's gate is not optional).
- Freehand retrieval only: never route through `ps query template` or
  `ps query catalog`; only the plugin's `cypher` MCP tool is used for
  execution.
- The skill reaches PS Service exclusively through the plugin's declared
  MCP connector (`policy-system-graph`) — never a direct graph connection,
  a repo-local script, or a spawned external binary, regardless of what
  might be available in a local checkout.
- Ground every Cypher clause in the fetched `psdomain://concepts`
  resource's actual property names, node labels, and edge directions —
  never invent one.
- Only read-only Cypher is ever authored. The Query Engine rejects writes
  and schema changes server-side — if a query is rejected for this reason,
  state the rejection plainly (per step 5's error-state table) and stop.
  Never attempt to route a write/schema-modifying clause around a
  server-side rejection by rewriting or resubmitting it disguised as
  read-only — a rejection is a stop condition, not a puzzle to route
  around.
- Do not author a rubric-gated fitness function or run an independent
  verification/re-query loop (step 1b, step 4) — these are scoped out of
  this skill entirely, not deferred. Falsification is the verification
  method; report its outcome plainly (survived vs. landed a contradiction)
  rather than hedging a clean run as still unverified.
- Falsification (step 7) always runs at least once — never skip it
  entirely, and never let it soften into confirmation-seeking. The number
  of attempts beyond the first is scope-conditional (see
  `references/falsification-step.md`'s attempt-cap determination), not the
  step itself. Follow `references/falsification-step.md`'s own guardrails:
  vary the attack angle between attempts, terminate at the cap or the
  first landed disproof, report every attempt.
- Don't silently collapse a compound question, force a same-node match
  across a non-convergent layer, or invent a status definition for an
  entity that has none — surface each as an explicit choice instead.
- The active-only default (see step 2) may be applied without asking, but
  never invisibly — it must always appear in the proposed question and the
  output's Filters line, and it never extends to `Policy`/`Standard`/
  `Control`, which still require an explicit lifecycle question.
- Never collapse the four error states in step 5's table into each other
  or into a generic "no answer" — an unreachable endpoint, a throttled
  request, a rejected query, and a graph that genuinely lacks the
  information are four different conditions the user needs told apart.
