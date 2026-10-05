# Falsification Step

This is a reusable instruction, not a skill (no on-load phase, no
independent user-facing entry point) — it is invoked by another skill
after that skill has already constructed a candidate answer. It follows
the same non-skill-instruction pattern as `reasoning.md`: read fresh each
time, never paraphrased from memory.

**Version:** 0.3.0

## Purpose

Attempt to disprove a constructed answer from the graph's own data,
rather than confirm it: ask whether the graph contains data that
contradicts the claim, not whether the claim's own query re-derives it.
For `ps-qna`, falsification is the verification method — there is no
separate rubric-gated fitness-function check. An answer that survives it,
within the attempt cap, is reported as verified; the signal's strength
scales with the cap used.

## Preconditions

The invoking skill must supply, verbatim: the approved question text, the
entities/edges it routes through, the constructed answer, and the
retrieved data/query the answer was built from. The domain concepts
already fetched at the invoking skill's own On Load step (via
`domain_concepts`) remain valid — **do not refetch them for this step.**
Never invoke this against an answer the user hasn't already seen as a
first-pass construction — falsification turns that first pass into a
verified answer (or surfaces a contradiction).

## Determine the attempt cap (scope-aware)

Before running any attempt, set `max_falsification_attempts`:

- **5** — the supplied Entities list includes `Policy`, `Standard`, or
  `Control`, or the user asked for deeper scrutiny. These three are
  actively revised through governance workflows, unlike the ingested,
  read-only `RegulatoryInstrument`/`Requirement` layer — so a
  construction-step error is most plausible here.
- **1** — otherwise (the ingested compliance spine: `RegulatoryInstrument`,
  `Role`, `Requirement`, `Obligation`, `Capability`, or the classification
  layer `PracticeArea`/`RiskPath`).

This is a floor, not a skip — every question gets at least one
adversarial attempt. Pilot rounds found every landed disproof on attempt
1 for the ingested spine; the Policy/Standard/Control layer hasn't been
tested the same way, so it defaults to the full cap instead.

State which cap applies, and why, before running attempt 1.

## Process

1. **Author adversarial queries freehand.** Write ad hoc Cypher,
   genuinely attempting to find data that contradicts the constructed
   answer — not a fixed taxonomy of checks. Vary the angle each attempt
   (edge direction, broader scope, an excluded filter, a sibling entity)
   — repeating the same weak angle in different words doesn't count as a
   new attempt ("confirmation theater").
2. **Execute each query** via the `cypher` MCP tool on the same PS
   Service connector (`ps-mcp`) the invoking skill's retrieval already
   used — never a subprocess, script, or similarly-named connector
   lacking `domain_concepts`. Apply the invoking skill's named
   error-state distinctions to any non-success result here too — a
   failed-to-execute attempt is not evidence either way.
3. **Judge each attempt** against the answer's actual claim: **Landed** —
   data contradicts the answer, stop immediately; **Missed** — consistent
   with the answer or no contradicting data, continue.
4. **Terminate** at whichever comes first: `max_falsification_attempts`
   (**1** or **5**), or the first landed disproof.

## Output shape

Falsification hands its result back to the invoking skill: attempt
count, landed-or-none-landed, cap used, and (if landed) what it
contradicts — folded into the one confidence line. The full attempt list
is available only via the on-request detail handler, never printed here.

## Guardrails

- Never skip this step, regardless of scope — only the cap above 1 is
  scope-conditional; the 1-attempt floor always applies.
- Report the outcome plainly: "verified — survived falsification" if
  nothing landed, or the contradiction itself if one did.
- Never stop early because an attempt "probably" would miss — run it, or
  don't claim it was attempted.
- Never report a miss as a landed disproof, or soften a landed disproof
  into a caveat — report exactly what the query returned.
- Ground every Cypher clause in the fetched domain concepts' actual
  property names, node labels, and edge directions — never invent one.
