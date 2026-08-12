---
name: policy-question
description: >-
  Use when a user poses an open or vague question about the Policy System
  compliance graph. Socratically identifies the user's actual intent, grounds
  it against the ps-domain conceptual model, and collaboratively refines it
  into a single scoped, answerable question, annotated with the entities and
  relationships it routes through. Then runs freehand Cypher retrieval and
  constructs a first-pass answer. Does not author a rubric-gated fitness
  function, run an independent verification loop, or attempt falsification —
  those remain separate, not-yet-added increments (spikes/pipeline2).
metadata:
  copyright: "© 2026 Cartman ApS. All rights reserved."
  version: "0.3.0"
  tags: [thinking, reason, help, retrieval]
---

# Policy Question Refinement

## Purpose

Turn an open/global question into a single, scoped, answerable question,
grounded in the Policy System's actual entities and relationships — not the
user's assumed vocabulary — then run freehand retrieval against the graph
and construct a first-pass answer. This covers step 1a (narrowing) plus
steps 2-3 (freehand retrieval, answer construction) of the Guided Fitness
Pipeline (spikes/pipeline2/README.md). It is an incremental extension, per
PROGRESS.md D13: fitness-function authoring (step 1b), the independent
verification loop (step 4), and falsification (step 5) are deliberately
**not** included here — each is a separate future increment, added and
tested on its own before the next is layered in.

**Deliverable:** the refined question text (approved verbatim by the
user), the entities/edges it routes through, the freehand Cypher query
used to retrieve data, the retrieved data, and a constructed first-pass
answer explicitly flagged as unverified (no fitness check, no
falsification yet).

## On Load

1. If the user hasn't provided a question, ask for one before doing anything
   else.
2. Read `docs/artifacts/ps-domain-concepts.md` in full — the only source of
   truth for entities, relationships, and vocabulary this refinement grounds
   against. Do not rely on memory of the schema from a prior turn.

## Core Principles

- Socratic method: one targeted question at a time; wait for the answer.
- Every narrowing move must tie back to a real entity/edge in
  ps-domain-concepts.md — never invent or assume vocabulary the model doesn't
  have.
- Adjust friction to the user: fewer questions when intent is already clear,
  more when the question is genuinely ambiguous.
- Narrow until answerable, not until trivial — stop as soon as the mapping to
  entities/edges is unambiguous.
- Retrieval is genuinely freehand: write ad hoc Cypher grounded in
  ps-domain-concepts.md's actual property names and edge directions — never
  route through `ps query template`/`ps query catalog`, and never invent a
  property or relationship the model doesn't have (mirrors README D10).

## Process

1. **Restate intent.** In one sentence, state back what you understand the
   user is actually trying to learn. Confirm or correct before proceeding.
2. **Socratic narrowing loop.** Ask one question at a time to resolve, only
   where still ambiguous:
   - Which entity type(s) the question is really about (Regulation, Role,
     Requirement, Obligation, Capability, Policy, Standard, Control,
     PracticeArea, RiskPath).
   - Which relationship(s)/traversal direction the question implies.
   - Scope bound (one regulation vs. all; one capability vs. org-wide).
   - Counting unit, if the question asks "how many" (e.g. distinct Controls
     vs. distinct chains — these yield different numbers over the same
     graph).
   - Status/lifecycle bound, if relevant (current-only vs. including
     deprecated/superseded/planned) — check the entity's own Properties
     table in ps-domain-concepts.md first: if it carries no status/
     lifecycle property itself (e.g. Obligation), say so and confirm with
     the user how "active-only" should be defined transitively through its
     chain, rather than improvising a definition silently. If more than one
     entity in that chain carries its own independent status property (e.g.
     both Regulation.status and Requirement.status exist separately), don't
     silently pick one — ask whether "active-only" should key off the
     regulation, the requirement, or require both, since they can diverge
     (a Requirement can be individually deprecated while its Regulation
     stays active).
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
     (PracticeArea, RiskPath — see ps-domain-concepts.md's Document
     Purpose section) has a real matching category (e.g. RiskPath.
     risk_type) that could anchor the question instead of a bare
     keyword filter on Capability/Obligation text.
   - Whether the question implies comparing or matching entities across a
     layer the model deliberately keeps non-convergent (Role, Standard,
     Control — check the concept's own Identity note in
     ps-domain-concepts.md). If so, confirm with the user whether
     relocating the comparison to the nearest convergent layer (typically
     Obligation or Capability) satisfies their intent — state explicitly
     that the comparison is being relocated, not answered directly at the
     layer they named.
3. **Propose the refined question** in plain English, together with the
   entities/edges it maps to. If step 2 surfaced a compound question the
   user chose to keep bundled, state each clause explicitly in the
   proposal rather than collapsing them into one sentence.

   **Pre-flight gate, mandatory, not discretionary:** if the proposal
   applies any status/lifecycle filter (e.g. "active-only"), confirm every
   independently-statused entity in the filtered chain was explicitly
   asked about in step 2 — not silently defaulted. If it wasn't asked, go
   back and ask before proposing. This is a checklist item to verify, not
   a judgment call about whether the ambiguity "feels" significant enough
   to raise.
4. **Approval gate.** Ask the user to approve the question verbatim. If not
   approved, continue the loop on the specific part that's wrong — don't
   restart from scratch. Only continue to step 5 once approved.
5. **Freehand retrieval.** Write ad hoc Cypher against the graph to answer
   the approved question — genuinely freehand, not assembled from a
   template library. Execute it via:

   ```
   /usr/bin/python3 spikes/e2e-pipeline/ps.py cypher "<QUERY>"
   ```

   (read-only guarded, `localhost:6379`, graph `policy_system` — reuses
   `ps.py`'s connection pattern and read-only guard directly, per
   spikes/pipeline2/README.md's Setup step 3; does not route through
   `ps query template` or `ps query catalog`). Show the query before or
   alongside its results — never hide what was actually run.

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
6. **Construct the answer.** Build a plain-English answer directly from the
   retrieved rows. State only what the data supports; do not round up,
   extrapolate, or fill gaps with assumed domain knowledge.
7. **Output**, in this shape:

   ```
   Question: <refined question text>

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

   Query:
   <the executed Cypher>

   Retrieved data:
   <rows, or a compact summary if large>

   Answer: <constructed answer>

   Status: unverified — no fitness-function check or falsification pass
   yet (spikes/pipeline2/PROGRESS.md D13). If the answer was produced via
   an attribute/theme filter rather than a modeled traversal (Edges line's
   second or third case), add a second sentence naming that the result is
   also sensitive to the specific keyword/theme terms chosen — not just
   unverified in the fitness-check sense. A different keyword list could
   return a different result set.
   ```

## Guardrails

- Never silently substitute a different entity/edge than what the user's
  language implies — ask, don't assume.
- Don't let narrowing drift the question away from the user's original
  intent — if scope has shifted substantially since step 1, say so and
  re-confirm intent before continuing.
- Never execute retrieval before the user has approved the refined question
  verbatim (step 4's gate is not optional).
- Freehand retrieval only: never route through `ps query template` or
  `ps query catalog`; only `ps cypher`'s connection pattern and read-only
  guard are reused.
- Ground every Cypher clause in `ps-domain-concepts.md`'s actual property
  names, node labels, and edge directions — never invent one.
- Do not author a rubric-gated fitness function, run an independent
  verification/re-query loop, or attempt falsification — these are
  separate, not-yet-added increments. Always present the constructed
  answer as unverified, never as confirmed correct.
- Don't silently collapse a compound question, force a same-node match
  across a non-convergent layer, or invent a status definition for an
  entity that has none — surface each as an explicit choice instead.
