# Falsification Step

This is a reusable instruction, not a skill (no on-load phase, no
independent user-facing entry point) — it is invoked by another skill
after that skill has already constructed a candidate answer. It follows
the same non-skill-instruction pattern as `reasoning.md`: read fresh each
time, never paraphrased from memory.

**Version:** 0.2.0

## Purpose

Attempt to disprove a constructed answer from the graph's own data, rather
than looking for confirmation. Falsification attacks the answer's framing:
it asks whether the graph contains data that contradicts the claim, not
just whether the claim's own supporting query re-derives it. For
`policy-question.md`, this is the skill's verification method — there is
no separate rubric-gated fitness-function check (see that skill's Purpose
section for why it's scoped out entirely, not deferred).

An answer that survives falsification (no attempt lands a contradiction,
within the attempt cap) is reported as verified by falsification — but the
strength of that signal scales with the attempt cap actually used: a clean
run at the 1-attempt floor is lighter evidence than one at the 5-attempt
cap, and the invoking skill's Status line should reflect which cap applied
rather than implying every clean run carries equal weight.

## Preconditions

The invoking skill must supply, verbatim:

- The approved question text
- The entities/edges the question routes through
- The constructed answer
- The retrieved data / query the answer was built from

Never invoke this against an answer the user hasn't already seen as a
first-pass construction — falsification is what turns that first-pass
answer into a verified one (or surfaces a contradiction), not a
post-hoc confirmation step layered on top of some other check.

## Determine the attempt cap (scope-aware, per spikes/pipeline3 D6)

Before running any attempt, set `max_falsification_attempts`:

- **5** — if the supplied Entities list includes `Policy`, `Standard`, or
  `Control`, or the user explicitly asked for deeper scrutiny on this
  question. These three are the customer-governed layer:
  `ps-domain-concepts.md` describes them (unlike `RegulatoryInstrument`/`Requirement`,
  which are ingested and read-only once created) as "created by policy
  managers through governance workflows" and actively revised — data this
  pipeline doesn't control the quality of, so it's where a construction-step
  error is most plausible.
- **1** — otherwise (the question stays entirely within the ingested
  compliance spine: `RegulatoryInstrument`, `Role`, `Requirement`, `Obligation`,
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
   tools/graph-query/ps.py cypher "<QUERY>"
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

The pipeline's verdict is exactly this: "verified — survived N
falsification attempts" (if none landed) or the contradiction reported
plainly (if one did). State the attempt cap alongside it, so the user can
weigh a 1-attempt clean run against a 5-attempt one appropriately.

## Guardrails

- Never skip this step entirely, regardless of scope — the 1-attempt floor
  applies even to questions confined to the ingested spine. Only the cap
  above 1 is scope-conditional, not whether the step runs at all.
- Report the outcome plainly: "verified — survived falsification" if
  nothing landed within the attempt cap, or the contradiction itself if
  one landed. Don't imply a scope or rigor beyond what was actually
  attempted — always state the attempt cap used and why, so a 1-attempt
  clean run isn't read as carrying the same weight as a 5-attempt one.
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
