---
name: policy-question
description: >-
  Use when a user poses an open or vague question about the Policy System
  compliance graph. Socratically identifies the user's actual intent, grounds
  it against the ps-domain conceptual model, and collaboratively refines it
  into a single scoped, answerable question, annotated with the entities and
  relationships it routes through. Does not retrieve data, author a fitness
  function, or answer the question itself.
metadata:
  copyright: "© 2026 Cartman ApS. All rights reserved."
  version: "0.1.0"
---

# Policy Question Refinement

## Purpose

Turn an open/global question into a single, scoped, answerable question,
grounded in the Policy System's actual entities and relationships — not the
user's assumed vocabulary. This is the narrowing half of the Guided Fitness
Pipeline's step 1 (spikes/pipeline2/README.md); fitness-function authoring is
a separate, later step, out of scope here.

**Deliverable:** the refined question text, approved verbatim by the user,
plus a short labeled list of the entities and edges (from
ps-domain-concepts.md) it routes through. No retrieval, no answer, no
fitness function.

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
     deprecated/superseded/planned).
3. **Propose the refined question** in plain English, together with the
   entities/edges it maps to.
4. **Approval gate.** Ask the user to approve the question verbatim. If not
   approved, continue the loop on the specific part that's wrong — don't
   restart from scratch.
5. **Output**, in this shape:

   ```
   Question: <refined question text>

   Entities: <Label, Label, ...>
   Edges: <EDGE_TYPE, EDGE_TYPE, ...>
   ```

   Nothing else — no answer, no fitness function.

## Guardrails

- Never silently substitute a different entity/edge than what the user's
  language implies — ask, don't assume.
- Don't let narrowing drift the question away from the user's original
  intent — if scope has shifted substantially since step 1, say so and
  re-confirm intent before continuing.
- Do not retrieve data, run queries, or answer the question.
- Do not author a fitness function.

## What This Is NOT

- Not the retrieval, fitness-authoring, verification, or falsification steps
  of the Guided Fitness Pipeline — those remain separate, unbuilt
  concerns (spikes/pipeline2/README.md, PROGRESS.md).
