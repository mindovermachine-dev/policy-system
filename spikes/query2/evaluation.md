# Evaluation: is Candidate D (and `q-approach4.md`'s synthesis) actually better?

Working through `q-approach4.md`'s "Next steps" list end to end, against the
live `policy_system` graph and real local models (not a scripted fake
client) — same discipline every prior mechanism in this spike was held to.
Code is in this directory; each claim below points at the script that
produced it, and every script can be re-run directly (`python3 <file>.py`).

**Short answer: yes, for a specific and now-measured slice of the problem.**
Candidate D (the pre-compiled catalog) fully and correctly solves 4 of the
12 questions `q-approach4.md` set out to solve — H1, H5, H9, H11 — with zero
LLM calls, in under 60ms, matching `golden-answers.md` exactly, and (for H1
and H11) demonstrably fixing failures the freehand agent still exhibits live
today. Candidate B (the DSL) turned out to be unnecessary — decided by
measurement, not deferred as a hunch. Candidate C (cross-model verification)
got one real, positive data point, not yet enough to adopt. 8 of the 12
questions are confirmed to still need `query_mechanism_v2`'s existing
agentic loop, unchanged — this design doesn't pretend otherwise.

## What was built

| File | What it is |
|---|---|
| [`mining-pass.md`](./mining-pass.md) | §7 fix 1/8: every golden query classified by chain shape, free-text need, and judgment need. Finding: one catalog root (two joined chains), not several — and no DSL primitive gap exists once that catalog exists. |
| [`catalog.py`](./catalog.py) | Candidate D's compiler — per-hop Python joins (not one multi-hop Cypher MATCH), a staleness signature, and `CatalogStore` (staleness-on-read, synchronous recompile). |
| [`experiment_catalog_cross_verify.py`](./experiment_catalog_cross_verify.py) | §7 fix 7: catalog output diffed row-for-row against golden values and against the live 6-hop query directly. |
| [`experiment_staleness.py`](./experiment_staleness.py) | §9 item 8: live mutation of a Control's status in FalkorDB, confirms detection + synchronous recompile, then reverts. |
| [`resolver.py`](./resolver.py) | §10.5 bake-off: lexical substring vs. TF-IDF cosine, plus a small curated acronym glossary. |
| [`experiment_resolver_bakeoff.py`](./experiment_resolver_bakeoff.py) | The bake-off itself, run against all 5 known NL-mapping cases (H3, H6, H8, H9, H11) with multiple real phrasings each. |
| [`catalog_answers.py`](./catalog_answers.py) | Deterministic answer functions for H1/H5/H9/H11 — no LLM narration, graded against `golden-answers.md`'s rubrics below. |
| [`query_mechanism_v3.py`](./query_mechanism_v3.py) | The fixed-order router: v1 template → Candidate D catalog → v2 agent (unchanged). No Candidate B, no Candidate C in the default path. |
| [`experiment_full_attribution.py`](./experiment_full_attribution.py) | All 39 questions run through the router, per-stage attribution reported. |
| [`experiment_before_after.py`](./experiment_before_after.py) | Live, same-model, same-question comparison: the old freehand v2 agent vs. the new catalog stage, on H1 and H11. |
| [`experiment_cross_model_verification.py`](./experiment_cross_model_verification.py) | §7 fix 3: Candidate C tested standalone, once, against a real documented gap. |

## 1. Mining pass: Candidate B is a no-go, measured not assumed

Every one of the 39 golden Cypher queries in `golden-answers.md` is a
contiguous sub-path of exactly two chains sharing one join point at
`Obligation`. There's no traversal shape a DSL would need to compile that
the catalog doesn't already materialize — so `q-approach4.md`'s own §7 fix
9 go/no-go gate for Candidate B resolves to **no-go**, decided here rather
than left open for a later report. Full classification (which questions
need free-text resolution, which need judgment beyond the joined rows) is
in `mining-pass.md`.

## 2. The catalog is correct, independently verified against the exact bug that bit v1 once

`compile_catalog()` deliberately never issues a multi-hop `MATCH` — it
walks each relationship type as its own single-hop query and joins in
Python, specifically to sidestep the FalkorDB column-projection bug
`golden-answers.md`'s M7 entry documents (a 6-hop `MATCH` silently returning
33 or 49 rows instead of 57 depending on which columns were projected).
`experiment_catalog_cross_verify.py` then checks that decision was actually
sound, not just theoretically motivated:

```
[Check 1]  GDPR requirement->...->Control chains: 481-row catalog gives 57, matches golden exactly
[Check 1b] is_current_evidence split: 31 current / 26 stale, matches golden exactly
[Check 2]  live 6-hop MATCH vs. catalog, diffed row-for-row on all 6 ids: 0 discrepancies
[Check 3]  ungoverned capabilities: 55 of 68, matches golden exactly
```

481 total rows compiled from the whole graph (68 capabilities, 342
obligations, 281 requirements, 5 regulations).

## 3. Staleness-on-read works, demonstrated live not just asserted

`q-approach4.md` §5 originally said Candidate D's catalog lookups were
"staleness-checked on read" as a forward reference to a section that hadn't
specified a mechanism yet — its own §6 critique flagged this as "a label,
not a mechanism." `experiment_staleness.py` closes that: mutates a real
Control's `implementation_status` live in FalkorDB (`implemented` →
`planned`), confirms `CatalogStore.get()` detects the signature change,
recompiles synchronously, and the `is_current_evidence` trust flag flips
`True`→`False` off the new data — then reverts the mutation and confirms the
revert. No node in this schema carries an `updated_at` timestamp (checked
directly against `query_mechanism_v2.py`'s `GRAPH_SCHEMA`), so the staleness
signature is a content hash over the 17-row Policy/Standard/Control layer
specifically — the volatile layer this whole spike exists to catch going
stale — not the originally-imagined `max(n.updated_at)`.

## 4. Entity resolution: lexical-first is mostly sufficient, with one measured, fixed gap

`experiment_resolver_bakeoff.py` ran lexical substring and TF-IDF cosine
against real phrasings for H3, H6, H8, H9, H11, M3. Result, largely
vindicating §10.5's literature read rather than overturning it:

- Full-phrase and expanded terms resolve correctly under **both** methods
  (SBOM, "multi-factor authentication," "logs access," "security logging").
- **Bare acronyms fail under both** — "MFA" and "PII" appear nowhere in the
  graph's own text, so neither method has anything to match against. This
  is a real, previously-unmeasured gap, not a hypothetical one from §10.5.
  Fixed with a small curated glossary (`ACRONYM_GLOSSARY` in `resolver.py`)
  rather than reaching for embeddings — the gap is "the term was
  abbreviated," not "the term is semantically distant," and a closed
  glossary for a small, stable compliance-acronym vocabulary is cheaper and
  more deterministic than a neural resolver for that specific problem.
- **Building the glossary fix introduced, and then caught, a real bug**: the
  first version normalized hyphens across the *entire* query before
  matching (to catch "multi-factor" style glossary keys), which also split
  "rate-limiting" into "rate limiting" — reintroducing false-positive
  word-overlap matches on H9, whose golden answer is specifically "no
  match." Caught by re-running the bake-off after the change, not assumed
  safe. Fixed to only touch words that actually hit the glossary.
- No embedding model was stood up for this — the local Ollama server would
  need restarting with `--embeddings`, a shared-infrastructure change this
  spike declined to make for a one-off comparison. TF-IDF is reported
  honestly as a term-weighting proxy, not a substitute claim for dense
  neural embeddings.
- **H8 remains genuinely unresolved by any single free-text call** — none
  of "stores customer PII," "PII," or "personal data storage compliance"
  reach any of the 5 golden capabilities under either method. This confirms
  `mining-pass.md`'s classification: H8 is a resolver-shape problem (needs
  an open top-k/multi-term approach), not solvable by widening the same
  single-lookup resolver, and correctly stays routed to `v2`.

## 5. Catalog answers graded against the real rubrics — all pass

`catalog_answers.py` produces deterministic, no-LLM answers for the 4
questions `mining-pass.md` found fully reachable. Graded against
`golden-answers.md`'s stated rubric criteria directly:

**H1** ("Are we compliant with GDPR Article 32?") — rubric requires:
partial-compliance verdict (2 clean / 1 partial / 1 stale / 2 ungoverned),
citing the real chain per sub-clause, flagging 32.4 as stale and 32.1/32.1d
as ungoverned. Output: `partial compliance (2 of 6 sub-clauses clean, 1
partial, 1 stale, 2 entirely ungoverned)` with every sub-clause enumerated
and correctly classified. **Matches exactly.**

**H11** ("missing MFA control") — rubric requires: NL→Capability mapping
stated explicitly, backward walk citing the real 7-obligation set, and a
caveat that the capability is currently governed/implemented so the
question is hypothetical. Output states the resolution (`'missing MFA
control' -> cap_access_control_authentication_151816`), lists exactly the 7
golden obligation ids across 3 regulations, and states "Currently governed
by ... CURRENT evidence. This is hypothetical against today's real
evidence, not an existing gap." **Matches exactly, including the caveat the
live freehand agent omitted (§6 below).**

**H5** (NIS2 update / stale Policies) — rubric requires: the real
`SUPERSEDED_BY` edge, and the deprecated/draft Policies flagged. Output
gives the real `HELVEX-SOP-1.0 → HELVEX-SOP-2.0` edge, explicitly states no
NIS2 version supersession exists yet, and lists both non-approved Policies.
**Matches exactly.**

**H9** (rate-limiting) — rubric requires: state plainly the graph doesn't
model this Capability, no verdict computed. Output, for the actual
question-catalog phrasing ("missing rate-limiting on an endpoint"):
"No Capability in the graph resembles ... The graph does not model an API
rate-limiting/throttling Capability." **Matches exactly** — with an honestly
documented fragility: the *unhyphenated* two-word form ("rate limiting")
still produces noisy false-positive candidates under the current lexical
resolver, flagged directly in the output text itself ("review before
trusting") rather than hidden. This is a real, reported limitation, not a
clean sweep.

## 6. Live before/after: the same model, the same questions, two mechanisms

`experiment_before_after.py` ran `qwen3-coder-next:q4_K_M` (the model
`q-approach2.md`/`direction-correction.md` found most capable) through the
**unmodified** `query_mechanism_v2` freehand agent on H1 and H11, live,
right next to Candidate D's answer for the same question:

| | H1 (before) | H1 (after) | H11 (before) | H11 (after) |
|---|---|---|---|---|
| Result | **Failed to converge** — no final answer after 8 turns (29.3s) | Correct partial-compliance verdict (0.05s) | Cited all 7 real obligation ids correctly (15.8s, 5 tool calls) | Same 7 ids (0.004s) |
| Rubric-required governance caveat | N/A (no answer) | Present | **Missing** — presents the MFA gap as a live violation with no mention that the capability is currently governed by an approved Policy with an implemented Control | Present |

H1's non-convergence reproduces `union-of-n.md`'s own documented finding
that `qwen3-coder-next` sometimes fails to converge on this exact question.
H11's result is the sharper finding: **the freehand agent got the citation
set completely right this run** (all 7 real ids, correctly organized) —
and *still* silently dropped the one rubric requirement most likely to
change a reader's risk assessment (that this is a hypothetical scenario
against a currently-passing control, not an active gap). That's not a
retrieval failure or a hallucination; it's a narration completeness gap
that persists even when everything else about the run goes right — exactly
the shape of defect `q-approach4.md` §2 says "more LLM judgment" doesn't
reliably fix. Candidate D never has this failure mode for these two
questions, because there's no narration step at all.

## 7. Full 39-question routing, and a bug this run caught live

`experiment_full_attribution.py` confirms the router handles all 39
questions exactly as `mining-pass.md` predicted:

```
v1-template:               24  (unchanged)
v2-catalog (Candidate D):   4  (H1, H5, H9, H11 — newly deterministic)
v2-agent (needs LLM):      11  (M3, M5, M14, H3, H6, H8, H10, H12, H13, H14, H15)
```

LLM-dependent question count drops from 15 (every rubric/schema-gap question
in `query1`'s baseline) to **11** — a real, measured 4-question reduction,
not the full 12 `q-approach4.md`'s original routing table optimistically
assigned to stages 2+3 combined, because H3 and H8 turned out (§4 above) to
need genuine reasoning/open-candidate-set handling the catalog alone
doesn't provide.

**Building this router surfaced a real bug, caught by actually running all
39 questions rather than only the 4 the catalog was designed for**: the
first version of the H1 catalog-template regex (`compliant with (\w+)
article\s*([\d.]+)`) also matched H3 ("Is *this* new API endpoint...
compliant with GDPR Article 32?") — a scenario-scoped question with a
different, two-capability answer — and would have silently returned H1's
org-wide verdict for it. This is precisely the "more stages, more surface
for a silent bug" risk `q-approach4.md` §6 named in the abstract, caught
concretely rather than left as a hypothetical. Fixed by anchoring the
pattern to "are we compliant" specifically (see `query_mechanism_v3.py`'s
comment on the fix).

## 8. Candidate C: one real catch, not yet enough to adopt

`experiment_cross_model_verification.py` gave `gemma4:12b` (a different
model family from the generator, `qwen3-coder-next`) the real trace + real
H11 answer from §6 above, and one narrow, falsifiable question: does this
answer state the capability is currently governed/implemented. Verdict:
**`NO`** — correctly matching direct inspection of the generator's answer,
which never mentions current governance status anywhere. One real catch, on
one case — consistent with `q-approach4.md` §7 fix 3's bar for *not yet*
wiring this into the router (n=1 is evidence worth continuing to test, not
evidence sufficient to adopt; §10.6's recommended 2–3-verifier ensemble
still untested).

## Net assessment

`q-approach4.md`'s own design goals (§2), checked against what actually got
built and measured, not just argued for:

- **"Eliminate failure classes deterministically wherever possible"** — done
  for H1 and H11 specifically: §6 shows a live failure (non-convergence) and
  a live near-miss (complete citations, missing caveat) on the *current*
  freehand mechanism, replaced by a catalog answer that cannot exhibit
  either failure mode because there's no generation step left to fail.
- **"Don't reach for more LLM judgment as the default fix"** — respected:
  Candidate B wasn't built at all once mining-pass.md showed it wasn't
  needed; Candidate C stays out of the default router pending more evidence
  than one case.
- **"Cost/latency is a first-class constraint"** — answered concretely, not
  deferred again: catalog answers run in single-digit milliseconds at zero
  token cost; the freehand agent took 15–30+ seconds per single (non-union)
  run in this same session, and union-of-3 (`query_mechanism_v2`'s existing
  default) triples that exposure.
- **"NL-to-Capability mapping needs to be a real, testable component"** —
  built (`resolver.py`), tested against 5 real cases with multiple
  phrasings each (not asserted), and its actual limits (bare acronyms,
  open multi-capability sets) are now documented rather than assumed away.

What this doesn't claim: M3, M5, M14, H3, H6, H8, H10, H12–H15 are
**unchanged** — still routed to the existing, already-tested `v2` agent,
because the mining pass and the resolver bake-off both independently
confirmed they need judgment or open-ended resolution the catalog
structurally can't provide. That's 8 of the original 12 target questions
still costing what they cost today. The honest scope of this result is
**4 questions moved from probabilistic-and-sometimes-wrong to
deterministic-and-verified-correct, at a measured 0-cost/near-0-latency**,
plus one confirmed negative result (Candidate B unneeded) that saves the
cost of building it, plus one real bug pattern caught by the router's own
first live use.
