# Falsification Step

This is a reusable instruction, not a skill (no on-load phase, no
independent user-facing entry point) — it is invoked by another skill
after that skill has already constructed a candidate answer. It follows
the same non-skill-instruction pattern as `reasoning.md`: read fresh each
time, never paraphrased from memory.

**Version:** 0.2.0

## Purpose

Attempt to disprove a constructed answer from the graph's own data, rather
than looking for confirmation. Where a fitness function (not yet built —
see `spikes/pipeline2/README.md` step 1b/4) would verify an answer *within*
the question's framing, falsification attacks the framing itself: it asks
whether the graph contains data that contradicts the claim, not just
whether the claim's own supporting query re-derives it.

This does not replace or imply a fitness-function check. An answer that
survives falsification is still, separately, unverified in the
fitness-check sense — say both things, never conflate them.

## Preconditions

The invoking skill must supply, verbatim:

- The approved question text
- The entities/edges the question routes through
- The constructed answer
- The retrieved data / query the answer was built from

Never invoke this against an answer the user hasn't already seen framed as
"unverified" — falsification is additive evidence, not a gate that turns
unverified into verified.

## Determine the attempt cap (scope-aware, per spikes/pipeline3 D6)

Before running any attempt, set `max_falsification_attempts`:

- **5** — if the supplied Entities list includes `Policy`, `Standard`, or
  `Control`, or the user explicitly asked for deeper scrutiny on this
  question. These three are the customer-governed layer:
  `ps-domain-concepts.md` describes them (unlike `Regulation`/`Requirement`,
  which are ingested and read-only once created) as "created by policy
  managers through governance workflows" and actively revised — data this
  pipeline doesn't control the quality of, so it's where a construction-step
  error is most plausible.
- **1** — otherwise (the question stays entirely within the ingested
  compliance spine: `Regulation`, `Role`, `Requirement`, `Obligation`,
  `Capability`, or the classification layer `PracticeArea`/`RiskPath`).

This is a floor, not a skip — every question gets at least one adversarial
attempt regardless of scope. `spikes/pipeline3/smoke-test/run-01-falsification-pilot.md`
and `run-02-harder-pilot.md` found every landed disproof and every
non-landing-but-materially-improving attempt, across both rounds, happened
on attempt 1 — a 1-attempt floor is evidence-backed, not an arbitrary
minimum, for the layer those pilots actually tested (the ingested spine).
It has not been empirically tested against the Policy/Standard/Control
layer, which is exactly why that layer defaults to the full cap instead of
inheriting the floor.

State which cap applies, and why, before running attempt 1.

## Process

1. **Author adversarial queries freehand.** Write ad hoc Cypher against the
   graph, genuinely attempting to find data that contradicts the
   constructed answer — not a fixed taxonomy of checks. Vary the angle
   between attempts (e.g. a different edge direction, a broader scope, an
   excluded status filter, a sibling entity the answer didn't consider) —
   repeating the same weak angle in different words does not count as a
   new attempt and is exactly the "confirmation theater" failure mode this
   step exists to avoid.
2. **Execute each query** via the same guarded surface the invoking skill
   already uses:
   ```
   /usr/bin/python3 spikes/e2e-pipeline/ps.py cypher "<QUERY>"
   ```
   (read-only guarded, `localhost:6379`, graph `policy_system`). Show every
   query run, not just the ones that land.
3. **Judge each attempt** against the answer's actual claim, not a
   restated version of it:
   - **Landed** — the query returned data that contradicts the answer.
     Stop immediately; do not run further attempts once one lands.
   - **Missed** — the query returned data consistent with the answer, or
     no contradicting data. Continue to the next attempt.
4. **Terminate** at whichever comes first:
   - `max_falsification_attempts` as set above (**1** or **5**), or
   - The first landed disproof.

## Output shape

Append this section to the invoking skill's output, after its own
Answer/Status block:

```
Falsification: <N> attempt(s), <landed | none landed>
  1. <query intent, one line> — <landed: what it contradicts | missed>
  2. ...
```

Never collapse this into a verdict. The pipeline does not conclude "answer
is correct" — it reports "answer survived N falsification attempts" (if
none landed) or reports the contradiction plainly (if one did), and lets
the user judge what that means for the answer's reliability.

## Guardrails

- Never skip this step entirely, regardless of scope — the 1-attempt floor
  applies even to questions confined to the ingested spine. Only the cap
  above 1 is scope-conditional, not whether the step runs at all.
- No verdict inflation: never state or imply pass/fail, "confirmed,"
  "verified," or "correct" — only what was attempted and what was found.
- Never stop early because an attempt "probably" would miss — run it, or
  don't claim it was attempted.
- Never report a miss as a landed disproof or soften a landed disproof
  into a caveat — report exactly what the query returned.
- If every attempt across many invocations misses, that is evidence about
  this step's own falsification creativity, not evidence the answer is
  correct — flag this pattern back to whoever is reviewing run logs rather
  than treating a clean streak as validation.
- Ground every Cypher clause in `docs/artifacts/ps-domain-concepts.md`'s
  actual property names, node labels, and edge directions — never invent
  one, same discipline as the invoking skill's own retrieval step.
