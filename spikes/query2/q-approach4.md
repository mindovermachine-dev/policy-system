# Approach 4: Four Candidate Architectures, Critiqued and Synthesized

**Scope note, upfront** (same discipline `q-approach3.md` used): this is a
design document, not a build. It works through four genuinely different
candidate architectures for what's left unsolved after
[`query1`](../query1/), critiques each against `query1`'s *real* evidence
(re-runs against live models, not assumptions), synthesizes a combined
design, critiques that synthesis too, and fixes what the second critique
finds. Nothing here is wired into code. It sets the spec for whichever
build follows, in this new `query2` spike — following `q-approach1.md`/
`q-approach2.md`'s own pattern (spec → build → real-model test) rather than
skipping straight to code.

**Revision note**: this document originally worked through three
candidates (A/B/C), critiqued, synthesized, and externally validated them
(§10 below). Candidate D was proposed afterward, once §10's own finding
that multi-hop traversal itself is the persistently hard part suggested a
fourth, more direct strategy: stop asking a model to traverse at all for
the structural questions. It's folded in here — full critique, revised
synthesis, revised critique, revised fixes — rather than appended as an
afterthought, since it changes §5's routing table materially, not just
adds a footnote to it.

## 1. What "fully solve" means here, precisely

Not "cover more questions" in the abstract — against the actual 39-question
catalog and its computed golden values in
[`../query1/golden-answers.md`](../query1/golden-answers.md):

| Bucket | Count | Status after `query1` |
|---|---|---|
| Deterministic template match (`query_mechanism_v1.py`) | 24 | **Solved.** 39/39 test pass rate on this router's own scope; zero LLM cost. Not touched by this document — nothing here should make this floor worse. |
| Genuine schema gaps (H10, H15) | 2 | **Out of scope for any query mechanism.** No `Service`/`System` node, no status-transition history exists in the graph. Flagged, not routed around, per `README.md`'s own discipline. |
| Extraction-scope gap (M3) | 1 | **Out of scope for this document.** NIS2/GDPR were never extracted with a distinct "Security Logging" capability to converge on — a data-extraction fix, not a query-mechanism fix. |
| Needs semantic/multi-hop reasoning | 12 | **The actual target of this document.** M5, M14, H1, H3, H5, H6, H8, H9, H11, H12, H13, H14. |

So "fully solve the query problem" in this document means: get all 12
remaining reachable questions to reliably pass their rubric — not "produce
a plausible-sounding answer once" — at a cost/latency that's honestly
justified against `query1`'s own repeatedly-stated bar (v1's free
deterministic floor), without reintroducing the specific failure classes
`query1` already caught red-handed on live models:

1. Relationship-direction reversal (closed by `direction-correction.md` —
   any new design must not regress this).
2. Property/id-pattern mistakes (partially closed by grounding the schema
   in the system prompt — `q-approach2.md`'s "Grounding location matters").
3. Stopping early / under-citing retrieved rows (H11's 3-of-7,
   `qwen3-coder-next` dropping 2-of-7 despite retrieving them) — the
   still-open problem four combination experiments and `union-of-n.md`
   partially addressed but did not close.
4. Whole-graph synthesis hallucination (H13's fabricated control counts,
   twice, in two separate re-runs, on different specific numbers each
   time).
5. Sampling-induced non-convergence (`union-of-n.md`'s H1/`qwen3-coder-next`
   3-for-3 turn-limit failure — a *new* risk `query1` itself introduced and
   never resolved).
6. Same-model validator blind spot (`experiment_validator_grounding.py` —
   giving the validator the same schema fix that helped the generator did
   not change its verdict at all).
7. NL-to-Capability mapping has never actually been built as a dedicated
   component — every doc through `query1` describes it as *needed* (H3,
   H8, H9, H11's rubrics all require it) and grades it via rubric, but
   `query_mechanism_v2.py` has no resolver for it; the agent does it ad hoc,
   inline, via `list_entities` plus its own judgment — exactly the kind of
   unconstrained inference that produced failures #2 and #3 above elsewhere.

## 2. Design goals distilled from that evidence

- **Eliminate failure classes deterministically wherever query1 showed
  that's possible**, rather than reduce their *rate* with more prompting.
  This is what closed #1; nothing has done this for #2, #3, or #7 yet.
- **Don't reach for more LLM judgment as the default fix.**
  `q-approach2.md`'s four combination experiments and
  `experiment_validator_grounding.py` all found the same shape of result:
  an added LLM step (synthesis, validation, revise-loop) lost information
  that was mechanically present in its own context rather than recovering
  it. Any new design proposing an LLM step has to justify why *this* step
  is different, not just add one because the target is "smarter reasoning."
- **Cost/latency is a first-class constraint, not an afterthought.**
  `union-of-n.md` tripled turn-budget exposure and converted a working (if
  flawed) single run into a 3-for-3 failure at least once. A design that
  spends more compute has to show what it buys, the same bar `query1`'s own
  "Next steps" already named and never finished ("the cost/latency
  comparison against approach 1's free deterministic floor").
- **NL-to-Capability mapping needs to be a real, testable component**, not
  an emergent side effect of an agent calling `list_entities` and guessing.
  Four of the twelve remaining questions depend on it (H3, H8, H9, H11).
- **Whole-graph narration (H12–H14) is the one LLM-in-the-loop pattern that
  hasn't shown a *structural* failure** — its one documented defect
  (H13's hallucinated counts) was a narration-accuracy slip on
  pre-computed, correct numbers, not a wrong-data or wrong-traversal
  problem. Worth preserving that shape, not discarding it along with
  everything else.

## 3. Four candidate architectures

These are deliberately orthogonal strategies for the same 12 questions —
not four variations on one idea.

### Candidate A — Closed-Library Classifier

Extend `query_mechanism_v1.py`'s own idea rather than replace it: keep the
model out of Cypher entirely. A small classifier step (LLM call, structured
output only — one label from a fixed enum plus extracted parameters) picks
one of a **closed library** of pre-authored, parameterized multi-step
pipelines (a superset of v1's single-step templates — e.g. H11's pipeline
is "resolve free text → capability id, then walk `Capability` backward to
`Obligation` to `Role` to `Regulation`, three chained but still
author-written queries"). The model never sees raw graph rows and never
writes Cypher; its only two jobs are (a) which pipeline, (b) what
parameters. Free-text-to-entity resolution (the actual NL-to-Capability
step) is a dedicated component: embed all `Capability.name` +
`Capability.description` values once, embed the free-text query fragment,
return top-k by cosine similarity, and only hand the classifier those k
candidates to disambiguate from — never the full unfiltered vocabulary.

**Worked example — H11** ("missing MFA control"): classifier recognizes
the "reverse-obligation-lookup" shape → embedding resolver returns
`cap_access_control_authentication_151816` as the top candidate for "MFA"
→ pipeline runs the exact backward chain query already in
`golden-answers.md` → result formatted directly from the query's own rows,
no free narration of *which* rows to include.

**Addresses**: #2, #3, #7 by construction — there is no freehand Cypher to
get wrong and no raw-row dump for a model to selectively under-cite from,
because the pipeline's `RETURN` clause is author-written to include
everything relevant, same as v1's `is_current_evidence` discipline.

**New risk it introduces**: the library is closed. A question whose shape
isn't in the library gets no answer at all (a clean refusal, like v1's
`NO_TEMPLATE_MATCH`) rather than a worse one — but M14 and H6 are exactly
the kind of question that resists a pre-authored pipeline: M14 needs a
judgment call ("is this draft Policy's governed Capability *actually*
GDPR-relevant") that isn't a traversal shape, it's a semantic filter with
no fixed Cypher predicate behind it.

### Candidate B — Semantic-Parse-to-DSL with Deterministic Compilation

Don't hand-author every pipeline (Candidate A's limit); instead give the
model a small **compositional grammar** of primitive operations —
`walk(from_label, rel_type, to_label, direction)`,
`filter(prop, op, value)`, `aggregate(fn, group_by)`,
`resolve_entity(label, free_text)`, plus an explicit `no_match` terminal
the grammar must accept as a legal output of `resolve_entity` (added per
§10.3 — a grammar with no honest "nothing fits" output risks forcing a
fabricated id where the current freehand system prompt's rule 2 currently
allows an honest refusal) — and have it emit a JSON plan built
from those primitives, not Cypher text. A deterministic compiler turns the
plan into Cypher, consulting `SCHEMA_RELATIONSHIP_DIRECTIONS` (already
built in `query_mechanism_v2.py`) to fill in the *correct* direction and
the schema's real property names by construction — the model never
specifies direction or property strings at all, so #1 and #2 aren't
"corrected after the fact" the way `direction-correction.md` does today,
they're **structurally impossible to get wrong** because the grammar
doesn't expose the choice. After execution, one deterministic mechanical
check (`extract_entity_ids`, already built and tested in
`experiment_citation_completeness.py`) compares ids present in the
compiled query's result rows against ids cited in the model's final
answer. A gap triggers exactly **one** targeted re-ask — "you didn't
mention {ids}; are they relevant?" — not blind resampling.

**Worked example — H1** ("GDPR Art. 32 compliance"): model emits a plan
walking `Requirement→Obligation→Capability→Policy→Standard→Control` for
`req.id STARTS WITH "GDPR-1.0_req_art_32"`, filtered/annotated by status
the same way v1's trust flag already does; compiler renders it correctly
by construction; citation gate checks the final answer mentions both
`GDPR-1.0_req_art_32.1` and `...32.1d` — the two sub-clauses
`q-approach2.md`'s own H1 rerun found `qwen3-coder-next` missing — and
forces a single corrective pass naming them explicitly if it doesn't.

**Addresses**: #1 and #2 **only in their narrow, originally-documented
form** — a reversed arrow, a wrong property string. It does not address
the broader class of "syntactically and schematically valid but semantically
wrong" queries — see §10.1, which found this is the *dominant* remaining
error class in the closest independent benchmark available, not a
residual one. **This section's original "structurally impossible to get
wrong" claim is corrected as of §10** — it was true only for the two
narrow failure modes it was written against, not for correctness in
general. #3 gets a targeted, cheap, deterministic gate instead of 3x
resampling — but see §10.2 for a real blind spot in that gate that this
document didn't originally account for.

**New risk it introduces**: `experiment_citation_completeness.py` already
found real precision problems with exactly this mechanical-check idea when
tested standalone — false positives on `whole_graph_stats`-routed
questions, over-flagging legitimate abbreviated citations — and concluded
"don't adopt as built." Reusing it here inherits that same risk unless
those specific problems are designed around, not just re-tested and hoped
away. It's also untested whether the grammar is *expressive enough* for
every remaining question shape — invented here, not yet mined from the
golden queries that actually exist. And, per §10.2, even a perfectly
precise version of this check only verifies that an id was *mentioned* —
not that what the answer *says about* it is true, a gap external research
now gives a name to.

### Candidate C — Heterogeneous Ensemble + Cross-Model Verification

Don't constrain generation at all — keep today's `query_mechanism_v2.py`
agentic loop exactly as it is, freehand Cypher, direction-corrector and
all. Instead diversify *which model* runs it: sample across genuinely
different model families already available locally (`qwen3:14b`,
`qwen3-coder-next:q4_K_M`, and the untested `qwen3.6:35b-mlx`) rather than
the same model 3x the way `union-of-n.md` does. Then add a **cross-model
verifier**: a structurally different model (not the generator, not a peer
sample of the generator) reviews the trace and answer, checking each cited
claim against the tool-call results actually present in the trace — the
specific, cheap experiment `q-approach3.md` named as untested and
literature-backed ("agreement bias and verifier error both decrease when
the verifier is no longer the generator").

**Worked example — H1**: `qwen3-coder-next` generates an answer with a
false governance claim (the real defect `direction-correction.md`
documented in a prior re-run). `qwen3:14b`, seeing the same trace but not
having generated the claim itself, is asked only "does the trace support
this specific sentence, yes or no, cite the row" — a narrower, falsifiable
task than "is this answer complete," which is the task
`experiment_validator_grounding.py` already showed same-model validation
fails at even with the schema fix applied.

**Addresses**: #6 directly (the thing this candidate is built to test);
diversifying model families also gives #5 (non-convergence) a different
shape of mitigation — a `qwen3.6:35b-mlx` timeout doesn't imply a
`qwen3:14b` timeout the way three same-model samples correlate.

**New risk it introduces**: doesn't touch #2, #3, or #7 at all — a
freehand agent that writes a wrong property name or under-cites is
unaffected by which model verifies it afterward unless the verifier
happens to catch that specific defect, which is exactly the open question
this candidate is meant to test, not something it can be assumed to solve
going in. Highest cost of the three (two model loads minimum, sometimes
three, plus a verification pass) for a benefit that's plausible from
literature but has zero live evidence on this graph specifically — the
same gap `q-approach3.md` itself flagged and left open.

### Candidate D — Pre-Compiled Catalog (Materialized Denormalized View)

A different angle than A, B, or C: don't constrain, diversify, or verify
live traversal — **remove it from the query path entirely** for anything
structural. Periodically (or, per §10.9, on-demand at read time) run a
deterministic compilation pass that walks every chain root this domain
supports — the same shapes v1's templates already parametrize, but
unparameterized: a full extract, not "give me the one for X" — and
denormalizes them into a flat, queryable catalog. One row per fully-
resolved chain, every relevant property inlined (not just ids): e.g.
`regulation_id, role_name, obligation_id, obligation_text, capability_id,
capability_name, capability_description, policy_id, policy_status,
standard_id, standard_status, control_id, control_status,
control_next_review_date, control_evidence_ref, is_current_evidence` —
the last computed the same way v1's trust flag already is, not re-derived
by a model. Stored as a small in-memory/queryable structure (a table the
tool layer filters, not a wall of text — see §10.8), grouped by every root
a golden query anchors on (Capability, Role, Regulation, Policy), not just
one.

Querying it has two steps, neither of which is freehand graph traversal:
(1) resolve free text against the catalog's own name/description columns
(the same lexical-vs-embedding choice as Candidate A's resolver, §10.5's
bake-off applies identically here); (2) return every row matching the
resolved id — the *entire* relevant chain, already joined — for the model
to read and cite from, not explore hop by hop.

**Worked example — H11** ("missing MFA control"): resolver matches "MFA"
against the catalog's capability columns → returns
`cap_access_control_authentication_151816` → catalog lookup returns all 7
pre-joined `(regulation, obligation)` rows for that capability in one call,
already correctly walked backward because the catalog is denormalized —
"forward" and "backward" aren't different traversal risks once the join is
already done, they're just which column gets filtered. Same mechanism
handles H1's forward compliance chain and H3's single-endpoint version:
one lookup, not a live 6-hop walk.

**Addresses**: #1 and #2 **completely**, not narrowly — for anything the
catalog's roots cover, there is no live Cypher at all, so there is nothing
for a model to get backward or wrong. Strictly stronger than Candidate B's
guarantee (§10.1) because it doesn't compile a *per-question* plan through
schema-aware machinery that could itself still misfire — it reuses a
*pre-verified* artifact, the same trust model v1's 24 questions already
rely on. Meaningfully softens #3 by turning "explore until you've covered
enough" into "read this already-complete table," though see the critique
below for what it doesn't close. Reuses this spike's single most reliable
pattern to date (`whole_graph_stats` + v1's templates have zero documented
structural failures across every re-run in this spike) rather than
inventing a new one.

**New risk it introduces**: **staleness.** A "periodically compiled"
catalog in a system whose entire value proposition is catching stale
Policies/Controls risks becoming exactly the kind of artifact it's meant
to flag — a Control's status flipping from `planned` to `implemented`
between compilations means the catalog confidently serves the *old*
answer with no signal it's out of date. This is the sharpest new risk in
this document and is addressed head-on in §10.9 and §7. Beyond that: the
catalog is closed to whatever chain roots someone thought to materialize
(same brittleness already flagged for Candidate A, §4) unless mined from
real usage, not invented; the compile pass is new code producing exactly
the long-chain shape (`Requirement→Obligation→Capability→Policy→Standard
→Control`) that already hid a real silent bug in v1's own history (the
FalkorDB column-projection issue, `q-approach1.md`'s "Result" section) —
trusting it without the same independent per-hop cross-check that caught
that bug once would repeat the mistake, not avoid it; and it does nothing
for M14/H6/M5's judgment questions — a complete, correctly-joined table of
facts still isn't a semantic filter over those facts.

## 4. Critique of each

**Candidate A.** Strongest guarantee against the failure classes that
matter most (#1, #2, #3, #7) because it removes freehand generation
entirely for the questions it covers — the same reason v1's 24 questions
have never failed a single golden check across any re-run in this spike.
But it's brittle exactly where the remaining questions are hardest: M14
and H6 don't decompose into a fixed pipeline, they need a judgment call
inside the traversal ("is this capability *actually* GDPR-relevant,"
"is this coverage *actually* redundant") that a closed library can't
express without either (a) growing a bespoke pipeline per question — not
a mechanism, a lookup table with extra steps — or (b) quietly reintroducing
freehand reasoning at the one step that most needs constraining. Also
untested: whether the embedding-based entity resolver actually
outperforms the ad hoc `list_entities`-plus-judgment approach `query_
mechanism_v2.py` uses today, or whether a graph this size (68 capabilities)
is small enough that lexical substring matching (what v1's entity resolver
already does, successfully, for the simple tier) is sufficient and an
embedding pipeline is unjustified complexity.

**Candidate B.** The most *structurally* honest fix for #1/#2 — it doesn't
correct mistakes after the fact the way `direction-correction.md` does, it
makes them inexpressible, a stronger guarantee than anything shipped in
`query1`. But it inherits `experiment_citation_completeness.py`'s already-
documented precision problems wholesale unless explicitly redesigned
around them (that experiment's own conclusion was "don't adopt as built,"
not "needs a bit more testing"), and the grammar's coverage is asserted,
not measured — designed top-down from imagined operation types rather than
mined bottom-up from what the 39 golden Cypher queries in `golden-
answers.md` actually contain. If the real queries need a shape the grammar
doesn't have, this either silently under-serves those questions or grows
the grammar ad hoc until it's not meaningfully more constrained than
freehand Cypher, defeating its own premise.

**Candidate C.** The only candidate that directly tests a hypothesis
`q-approach3.md` already identified as worth testing rather than
theorizing further about — real value if it works, because it's the one
untested lever the spike's own research review flagged. But it's also the
only candidate built entirely on literature-based expectation with zero
project-specific evidence, which is precisely the posture `q-approach2.md`
warned against once already (the `GraphRAG-SDK` framework survey that
"initially recommended" a package whose real API didn't match its
documentation until someone actually installed and inspected it). Highest
cost, narrowest evidenced benefit, and doesn't touch three of the seven
named failure classes at all.

**Candidate D.** The strongest guarantee of all four against #1/#2 — not
because it constrains a live step better than B does, but because it
removes the live step, the same "eliminate, don't reduce the rate of"
standard §2 sets as this document's own bar. It also reuses this spike's
best-evidenced pattern (`whole_graph_stats`, v1's templates) rather than
proposing a new one, unlike A, B, and C. But its one new risk is a sharp
one *specifically for this domain*: a system built to catch stale
Policies and Controls cannot ship a query layer that itself silently
serves stale answers, and "periodically compiled" as originally proposed
doesn't resolve that on its own — it needs an explicit answer, not an
implicit one (§10.9 supplies one). It also inherits Candidate A's
closed-library brittleness (whatever chain roots aren't materialized
aren't answerable) and Candidate B's not-yet-measured-coverage problem
(which questions actually decompose into pre-joinable chains isn't
established until the same mining pass §7.1 already requires is run) —
and, like B, does nothing at all for M14/H6/M5's judgment questions.

## 5. Synthesis

No single candidate covers everything query1 left open, and forcing one to
(growing A's library indefinitely, or trusting C's literature-based
promise without a local test) repeats a mistake this spike has already
made once and corrected. The synthesis routes by what each candidate is
actually strong at, in a fixed order — cheapest, most-constrained, and
most-evidenced first, escalating only when a cheaper stage can't cover the
question. Candidate D, once added, absorbs most of what A and B were
separately reaching for (§10.8 gives this independent support — precompute,
don't traverse live, is the most externally-validated idea in this whole
document) — so it takes the primary escalation slot, and Candidate B is
demoted to a deferred, measured-not-assumed residual:

```
question
  │
  ▼
[1] v1 template router (unchanged)  ──── match ──→ answer (24 questions, $0, unchanged floor)
  │ NO_TEMPLATE_MATCH
  ▼
[2] Candidate D: pre-compiled catalog lookup — staleness-checked on read
    (§10.9), entity-resolved lexical-first (§10.5's bake-off), citation-
    completeness gate applied to its output too (§10.2's scope-limited
    version: catches omission, not false claims)
    (covers H8, H9, H11, and the structural/staleness-reasoning core of
    H1, H3, H5 — see §7.1's mining pass for exactly how much of H1/H3/H5
    this reaches)
  │ no catalog root/shape covers this question (measured, not assumed)
  ▼
[3] Candidate B: DSL-mediated agentic loop — DEFERRED. Built only if,
    after Candidate D is measured against the full 39-question catalog,
    a real residual gap remains that D's materialized roots don't reach.
    Not built speculatively alongside D.
  │ DSL plan invalid twice / genuinely open-ended judgment (M14, H6, M5)
  ▼
[4] v2's existing freehand agentic loop, unchanged, direction-corrector intact
    (the fallback of last resort — never worse than what query1 already has)
  │ rubric graded as a hard-tier compliance verdict specifically (H1 only)
  ▼
[5] Candidate C's cross-model verification, as a final gate — not a
    generation strategy on its own, and not applied to every question
  │
  ▼
[whole-graph, no entity to anchor on: H12, H13, H14]
  → whole_graph_stats (unchanged) + narration, same pattern query1 validated
```

Mapped to the 12 remaining questions:

| Question | Stage | Why |
|---|---|---|
| H8, H9, H11 | [2] Candidate D | NL-to-Capability resolved against the catalog's own columns, full pre-joined chain returned in one lookup — no live traversal risk at all, stronger than routing these through Candidate A's per-question pipeline |
| H1, H3, H5 | [2] Candidate D for the structural/staleness core; escalates to [3] only for whatever ad hoc filter shape the mining pass shows the catalog's materialized roots don't cover | The deep compliance chain is exactly what the catalog pre-joins; only a genuinely novel filter needs live reasoning at all |
| M14, H6 | [4] v2 unchanged | Genuine semantic judgment calls (GDPR-relevance filtering; redundancy detection) that resist a closed catalog, a closed pipeline, and a DSL grammar alike — routed to the one mechanism that already handles open-ended reasoning, honestly, not force-fit elsewhere |
| M5 | [4] v2 unchanged | Already documented in `example-questions.md` as needing semantic similarity over free text, not a structural join — same reasoning as M14/H6 |
| H1 specifically, additionally | [5] Candidate C gate | Highest-stakes rubric (a compliance verdict a Risk Manager acts on) gets the one extra, literature-motivated check, scoped narrowly rather than applied everywhere its cost isn't justified |
| H12, H13, H14 | `whole_graph_stats` + narration | Unchanged — the one pattern with no structural failure on record, only a narration slip |

## 6. Critique of the synthesis

Before writing this up as the final spec, running the same scrutiny this
document applied to the three candidates against the combined design
surfaces four real problems, not hypothetical ones:

1. **More stages is more surface for a silent bug**, the exact category
   `q-approach1.md` found by accident (the FalkorDB projection bug that
   silently dropped rows depending on which columns were `RETURN`ed).
   A 5-stage router with 3 new components (embedding resolver, DSL
   compiler, cross-model gate) has far more places for a `query1`-style
   silent-wrong-answer bug to hide than v1's single-stage template router
   ever did, and nothing in the design above says how each stage's
   correctness gets independently verified before the next stage is
   trusted to build on it.
2. **Candidate B's grammar is still asserted, not measured**, carried into
   the synthesis unresolved from its own critique in §4 — the synthesis
   assigns it H1/H3/H5 without first checking those three chains (plus the
   other 9 remaining golden Cypher shapes) actually decompose into the
   four primitives proposed (`walk`, `filter`, `aggregate`,
   `resolve_entity`).
3. **Candidate C's gate is wired in on faith.** §4 already flagged that C
   has zero project-specific evidence — the synthesis puts it in the
   pipeline for H1 anyway, repeating exactly the assumption-over-inspection
   mistake `q-approach2.md`'s `GraphRAG-SDK` misstep already taught this
   spike to avoid once.
4. **The escalation chain doesn't solve, and doesn't say it doesn't solve,
   the systematic-error case.** `union-of-n.md` already proved sampling
   more (Candidate C's premise, cost-wise) does nothing when every sample
   makes the identical mistake (H1/`qwen3:14b` missing the same umbrella
   clause three times). Nothing about routing through more stages changes
   that if the underlying model has a genuine knowledge/reasoning gap
   rather than a stochastic one — the design as written could be read as
   claiming to "fully solve" a class of error it structurally cannot
   touch.
5. **Candidate D's staleness risk is asserted to be addressable, not yet
   addressed.** §5 routes H1/H8/H9/H11 through a catalog and says it's
   "staleness-checked on read," but that's a forward reference to §10.9 —
   nothing in the routing table itself specifies *what* staleness signal
   gets checked or *when* a stale catalog gets refreshed versus just
   flagged. Left as written, "staleness-checked" is a label, not a
   mechanism.
6. **The catalog inherits v1's exact silent-bug precedent, and the
   synthesis doesn't yet require the specific check that would catch a
   repeat.** §7's existing "independent verification per new stage"
   (originally written for the embedding resolver, DSL compiler, and
   cross-model gate) doesn't explicitly name the one verification method
   that's already proven necessary for *this specific chain shape* — the
   per-hop independent join that caught the FalkorDB projection bug once.
   A generic "unit-test the compiler" isn't the same guarantee.
7. **Whether Candidate D actually obsoletes most of Candidate B is a claim,
   not yet a measurement.** §5 defers B pending "a real residual gap," but
   the synthesis doesn't yet specify what that measurement looks like or
   who runs it before code gets written for B — without that, "deferred"
   risks quietly becoming "built anyway because it's already speced."

## 7. Fixes applied

1. **Mine the DSL grammar from real golden queries before finalizing it.**
   Before Candidate B is built, extract the operation shapes actually
   present across all 39 Cypher queries already computed in `golden-
   answers.md` — not just H1/H3/H5, the full set — and check the proposed
   four primitives cover them. Any golden query that doesn't decompose
   cleanly is a signal the grammar is insufficient for that question, in
   which case that question routes to stage [4] (v2 unchanged) instead of
   being force-fit into a grammar that doesn't actually fit it. This
   changes M14/H6's routing from "assumed not to fit" (§5) to "verified not
   to fit" once the mining pass runs — the same standard `q-approach1.md`
   held itself to when it hand-verified S3's golden count instead of
   trusting an eyeballed number.
2. **Guarantee the chain can only add coverage, never subtract it.** Every
   stage failure (embedding resolver finds no confident match; DSL
   compiler can't produce a schema-valid plan after one retry-with-error)
   falls through to the *next* stage rather than failing the question —
   terminating at stage [4], today's already-tested v2 loop, as the floor.
   Concretely: this design is only adopted for a given question once it's
   shown, on the real golden-answers.md test harness, to strictly improve
   on (not just match) what `query_mechanism_v2.py` alone already scores —
   the same "beat the existing floor, don't just add complexity" bar
   `q-approach1.md` set for itself against a naive translator.
3. **Test Candidate C standalone before wiring it into anything**, exactly
   the way `direction-correction.md` and `union-of-n.md` both did before
   being trusted: a dedicated `experiment_cross_model_verification.py`,
   reusing the same live H1/H11 trials already on record (the false
   governance claim, the umbrella-clause omission, the MFA under-citing),
   with a genuinely different model family as verifier. Only if that
   experiment shows real catches — not just literature-predicted ones —
   does stage [5] belong in the routing chain at all. Until then, H1 exits
   at stage [4] as everything else does, and this document's own §5 table
   is provisional on that experiment's result, not a foregone conclusion.
4. **State the systematic-error limit explicitly, not implicitly.** This
   design does not claim to fix, and should not be read as claiming to
   fix, a defect that's identical across every sample/model tried — e.g.
   H1's missing GDPR umbrella clause, already observed to survive
   direction-correction, union-of-N, *and* (untested but predictable from
   the same root cause) would survive Candidate C's verifier too if the
   verifier shares the same underlying knowledge gap the generator does.
   The only real fix for that class, per `q-approach3.md`'s own research,
   is deeper retrieval/grounding (making sure the umbrella clause's row is
   actually pulled and prominently presented, not relying on any model,
   generator or verifier, to remember or infer it) — flagged as a genuinely
   open problem this document does not solve, not quietly assumed away.
5. **Independent verification per new stage before composition.** Each of
   the new components (entity resolver, catalog compiler, DSL compiler if
   built, cross-model gate) gets its own golden-value regression suite
   *before* being chained — following the same "unit-test in isolation,
   then verify end-to-end against live FalkorDB, then re-verify against
   real models" three-layer discipline `direction-correction.md` and
   `union-of-n.md` both already used, rather than trusting a multi-stage
   pipeline's correctness from integration behavior alone.
6. **Staleness-on-read, not periodic recompute, for the catalog.** Per
   §10.9's research (lazy validation at the point of use outperforms
   proactive periodic refresh for unevenly-changing data, which describes
   this graph's compliance-review-driven change pattern exactly), the
   catalog is checked against a cheap live staleness signal — e.g. a
   `max(n.updated_at)` or a monotonic write-counter over the labels it
   covers — at the start of every `ask()` call that would use it, and
   recompiled synchronously before answering if stale, not served stale
   with a caveat and not left to a fixed schedule. "Periodically compile"
   (the idea's original framing) is replaced with "compiled once, verified
   fresh on every read, recompiled inline when it isn't" — the graph is
   small enough (~700 nodes) that a synchronous recompile is the simpler
   design to reason about, not just the safer one.
7. **Cross-verify the compiled catalog the same way M7's golden chain was
   cross-verified**, specifically because the catalog's deepest row is the
   exact chain shape (`Requirement→Obligation→Capability→Policy→Standard
   →Control`, 5+ hops) that already hid a real silent bug in `query_
   mechanism_v1.py`'s own history: an independent per-hop Python-side join
   against live FalkorDB, diffed row-for-row against the compiler's
   output, not just a scripted unit test against a fake graph — the
   specific check that caught the original bug, not a generic substitute
   for it.
8. **Fold catalog-root mining into the same §7.1 mining pass**, not a
   separate assumption. The pass already required to mine Candidate B's
   DSL primitives from the 39 golden queries also determines which chain
   roots Candidate D's catalog needs to materialize (Capability, Role,
   Regulation, Policy, or others the mining reveals) — one empirical pass
   serving both candidates' coverage claims, rather than inventing each
   top-down.
9. **Defer building Candidate B on a measured trigger, not an assumed
   one.** Build and test Candidate D first, run it against the full
   39-question catalog per §9, and only scope/build Candidate B for
   whatever specific questions remain unanswered by D after that run —
   named explicitly in that run's report, not decided in advance here.
   This directly answers §6's point 7: "deferred" means a concrete
   go/no-go gate at a specific point in the build sequence, not a
   soft intention.
10. **Extend the citation-completeness gate to Candidate D's output, not
    only Candidate B's.** A model reading a catalog lookup's returned rows
    can still under-cite them in prose, the same way a model reading raw
    `run_cypher` rows could — the gate (and its §10.2-documented scope
    limit: catches omission, not false claims about a correctly-cited row)
    applies wherever a model narrates from retrieved rows, not just the
    one stage it was originally scoped to in this document's earlier
    draft.

## 8. Final design for `query2`

**Routing** (fixed order, each stage a hard fallback to the next on
failure, never a hard failure of the question itself):

1. `query_mechanism_v1.py`'s template router — unchanged, untouched, still
   the deterministic floor for 24 questions.
2. **New, primary**: pre-compiled catalog lookup (Candidate D) — chain
   roots mined from the 39 golden queries (§7 fix 8), staleness-checked
   and synchronously recompiled on read (§7 fix 6), entity-resolved
   lexical-first (§10.5's bake-off decides if embeddings are worth
   adding), citation-completeness gate applied to its narration (§7 fix
   10) — covering H8, H9, H11 and however much of H1/H3/H5's structural
   core the mining pass confirms.
3. **New, deferred**: DSL-mediated agentic loop (Candidate B) — built only
   if running stage 2 against the full 39-question catalog (§9) surfaces a
   real, named residual gap (§7 fix 9). Not built speculatively alongside
   Candidate D.
4. `query_mechanism_v2.py`'s existing freehand agentic loop — unchanged,
   direction-corrector intact — as the floor for M5, M14, H6, and anything
   stage 2/3 couldn't confidently handle.
5. **New, conditional on §7.3's experiment succeeding**: cross-model
   verification gate (Candidate C), scoped to H1 only, budgeted for at
   least two independent verifiers if it proceeds (§10.6) — not built at
   all if that experiment doesn't show a real catch.
6. `whole_graph_stats` + narration — unchanged, for H12/H13/H14.

**Explicitly not attempted here**: H10, H15 (schema gaps), M3 (extraction-
scope gap) — same discipline as every prior doc in this spike, flagged for
whoever owns the domain model or extraction methodology next, not routed
around with a query-mechanism trick.

**Explicitly not claimed to be fixed by this design**: systematic errors
identical across every model/sample tried on a question (§7.4) — a
retrieval/grounding problem, not a routing or verification one. Nor is
Candidate D's catalog itself immune to this: if the compile pass has an
undetected bug (the exact risk §7 fix 7 is built to catch), every question
routed through stage 2 inherits it identically, the same way a wrong
golden value would silently pass every test written against it.

## 9. Verification plan

Same bar every prior mechanism in this spike was held to, none of it
skipped:

1. **Per-component regression tests** (§7.5) for the embedding resolver,
   DSL compiler, and citation gate, each against fixed synthetic cases
   before touching live data — mirroring `test_query_mechanism_v2.py`'s
   existing pattern (scripted `FakeLLMClient`, no live model needed to
   verify plumbing).
2. **The grammar-mining pass** (§7.1) run and documented before Candidate
   B's scope is finalized — output is a table of which golden queries fit
   the primitive set and which don't, not an assumption.
3. **End-to-end against live FalkorDB**, the same way
   `direction-correction.md` verified its corrector against the real H1/H11
   reversed queries — every new component checked against real graph data,
   not just scripted fakes.
4. **Real live-model re-runs against `golden-answers.md`**, per question,
   before any stage is claimed to work — the discipline every fix in
   `query1` from the grounding fix onward was actually held to, and the
   one thing a "looks right" read of generated Cypher or a passing unit
   test has never been a substitute for in this spike.
5. **The standalone cross-model verification experiment** (§7.3), run and
   reported before stage 5 is built at all, not after.
6. **A full 39-question re-run of the composed router**, reporting per-
   stage attribution (which stage answered each question, same as
   `union-of-n.md`'s `runs_sampled`/`union_ids_added` transparency) so a
   regression in any one stage is visible rather than averaged away in an
   aggregate pass rate. **This run is also the go/no-go gate for Candidate
   B** (§7 fix 9) — its report must name, explicitly, any question the
   catalog didn't reach, not just an aggregate score.
7. **Independent per-hop cross-verification of the compiled catalog**
   against live FalkorDB (§7 fix 7) — the same discipline that caught the
   real FalkorDB column-projection bug in `query_mechanism_v1.py`'s own
   history, run specifically because the catalog's deepest rows are that
   exact long-chain shape, before the catalog is trusted for any question.
8. **A live staleness test** (§7 fix 6): mutate a fact the catalog depends
   on directly in FalkorDB (e.g. flip a `Control.implementation_status`
   from `planned` to `implemented`), then confirm a subsequent `ask()`
   call detects the catalog is stale and recompiles before answering,
   rather than silently serving the pre-mutation row. Without this test,
   "staleness-checked on read" (§5) is an unverified claim, not a
   demonstrated property.

## 10. External research check — what the literature says before coding starts

Following `q-approach3.md`'s own precedent (check whether the problem is
already solved, or whether the design's assumptions survive contact with
outside evidence, before writing code) — a literature check against §3–§7
above, done after the synthesis rather than before, on purpose: it's a
sharper test of a design to see what still holds once it's fully written
down, not just at the brainstorming stage. All findings below are dated
2025–2026 unless noted; several postdate this assistant's training cutoff
and were retrieved live.

### 10.1 The biggest hit: schema-correctness and answer-correctness are
nearly orthogonal, and Candidate B's central claim overstated how much
that gap matters

The closest independent benchmark to our exact problem — Text-to-Cypher —
reports a **Qwen3.5-9B baseline at 96.3% parse validity, 91.6% schema
validity, but only 18.9% exact execution accuracy**
([PIPE-Cypher](https://arxiv.org/html/2606.08481), 2026). Schema errors
(the class `direction-correction.md` already closed, and the class
Candidate B's compiler would close by construction) account for the
majority of *execution failures*, but a large share of remaining errors —
36.1% on a related benchmark — are queries that parse, use real schema
elements, execute cleanly, and still **answer the wrong operational
question**: the paper's own words for exactly the "semantically wrong but
syntactically clean" failure this document's §3 Candidate B did not
distinguish from the direction/property-name failures it was designed
against. Grammar-constrained decoding research independently confirms the
shape of the gap: constrained generation is "a format guarantee, not a
semantic guarantee" — the output is always structurally valid, never
guaranteed correct
([Grammar-Constrained Generation](https://tianpan.co/blog/2026-04-16-grammar-constrained-generation-output-reliability), 2026).

**Consequence for this design**: §3's Candidate B write-up (before this
section) claimed the DSL compiler makes #1/#2 "structurally impossible to
get wrong" without qualifying that this only covers the two narrow classes
it was built against (direction, property strings) — corrected in place
above. PIPE-Cypher's own answer to the exact same gap, worth noting
directly, was **not** more deterministic constraint — it was adding a
calibrated LLM judge over post-execution candidates plus schema-specific
few-shot retrieval examples, which alone moved mean accuracy from 0.036 to
0.200 in their ablation. That's a real, cheap, currently-missing lever —
**none of Candidates A, B, or C in this document mention few-shot
schema-grounded examples at all**, despite it outperforming pure
constraint-based approaches in the closest available benchmark.

### 10.2 A named failure mode that is a precise, independent description of
a bug we already found empirically — and it defeats the citation gate

A 2026 clinical-RAG study names **"deceptive grounding"**: a model cites
the *correct* entity, but makes a *false claim about it* — and this
"passes every existing evaluation framework because the failure operates
at the entity-attribution level, below the resolution of hallucination,
faithfulness, and citation checks"
([Deceptive Grounding](https://arxiv.org/html/2607.09349), 2026; measured
at 7.8% overall, 13.6% in a higher-stakes subset in that paper's domain).
This is not an abstract risk for us — it is an exact, independent name for
a defect `direction-correction.md` already caught live: H1/`qwen3-coder-
next`'s re-run made "a false governance claim about a correctly-cited
capability." The mechanical citation-completeness gate this document's
synthesis relies on as its first line of defense against under-citing
(§5, stage 3) checks only whether an id is *mentioned* — it is structurally
blind to whether the claim made about that id is *true*, by the same logic
that made the clinical study's checks blind to it. The broader
faithfulness-research consensus for 2025–2026 is claim-level entailment
scoring (does each generated sentence follow from the retrieved evidence),
not id-presence matching — but that requires an LLM-driven entailment
judgment, which reopens exactly the "another LLM step that can lose more
than it recovers" risk `q-approach2.md`'s validator experiments already
found. **This is a real, unresolved tension, not a simple omission to
patch**: the cheap deterministic gate we can build is known-incomplete;
the check that would close the gap is the kind of check this spike has
already found unreliable twice. §7's fixes list is amended to state this
gate's scope precisely (id-presence, not claim-truth) rather than implying
it is a general correctness check.

### 10.3 Constrained output can suppress honest "I don't know" and can
actively degrade reasoning quality if implemented naively

Two separable findings, both bearing directly on Candidate B: constrained
decoding "forces plausible-sounding fabrications rather than expressing
uncertainty" when the model doesn't actually know a value — a specific
threat to H9 (the graph genuinely has no rate-limiting capability; the
current freehand system prompt's rule 2 explicitly permits saying so,
and a DSL without an equivalent escape hatch risks losing that). Separately,
strict format constraints have been shown to degrade reasoning accuracy by
up to 27 percentage points on math benchmarks specifically because JSON
output forces the answer field before chain-of-thought reasoning completes
([Grammar-Constrained Generation](https://tianpan.co/blog/2026-04-16-grammar-constrained-generation-output-reliability), 2026). §3's Candidate B design is amended above with an explicit `no_match`
terminal for the first finding; the second finding means the DSL emission
step must let the model reason in free text *before* emitting the
constrained plan, never force the plan as the first tokens — not yet
reflected in a design decision anywhere in this document before this
paragraph, and now added as a hard requirement for whichever build follows.

### 10.4 Agentic exploration, not semantic parsing, is where the field's
revealed preference is trending for multi-hop KGQA — cutting against
Candidate B's framing, not just qualifying it

A 2026 paper, GraphWalker, built specifically for agentic multi-hop KGQA,
reports that agentic exploration outperforms single-shot semantic parsing
on multi-hop reasoning generally, and that the advantage is *more*
pronounced on larger, more complex graphs
([GraphWalker](https://arxiv.org/pdf/2603.28533), 2026) — which, if
anything, argues our graph being small doesn't obviously favor constrained
semantic parsing the way §3 assumed; if scale matters at all here, the
literature's direction cuts toward keeping exploration agentic, not
toward Candidate B's constrained single/few-shot plan. This sharpens (not
just echoes) `q-approach3.md`'s own open question about whether ToG/PoG's
machinery fits a graph this small — it suggests the more defensible
default, absent our own test, is to lean on stage 4 (v2's existing
agentic loop) more, and Candidate B less, than §5's routing table currently
does.

Separately: **SymAgent** ([arXiv:2502.03283](https://arxiv.org/abs/2502.03283),
WebConf 2025) is close in shape to what this document's own §5 synthesis
arrived at independently — a planner that decomposes a question and a
bounded executor restricted to predefined tools — and reports this working
even with weak 7B backbones. That's much closer to Candidate A's shape
than Candidate B's grammar. Per this spike's own established discipline
("adopt, don't re-research" — `direction-correction.md`'s own framing),
this should be read *before* building Candidate A's classifier from
scratch, not after.

### 10.5 Entity resolution: the literature mildly vindicates this
document's own unresolved critique of Candidate A, rather than
overturning it

Entity-linking research finds hybrid (embedding + lexical) beats either
alone, but also that pure fuzzy-string matching is often *sufficient* on
knowledge bases without messy, web-scraped surface forms — our 68
capability names are curated, not scraped. This doesn't invalidate
Candidate A's embedding resolver so much as confirm §4's own critique was
already asking the right question ("whether a graph this size... is small
enough that lexical substring matching... is sufficient and an embedding
pipeline is unjustified complexity") — now with independent support for
running that comparison as a cheap head-to-head test (v1's existing
lexical resolver vs. an embedding resolver, on the four known NL-mapping
cases H3/H8/H9/H11) before committing to an embeddings pipeline, rather
than assuming either wins.

### 10.6 Cross-model verification: real support, but underscoped in this
document's synthesis

Self-preference bias in same-family LLM judges is well-documented for
2026, and the standard fix — a judge from a different model family — is
exactly Candidate C's premise, real independent support for it. But the
2026 production pattern found in this research is a **3-judge ensemble
across families with majority/weighted vote**, not the single verifier
§5's stage 5 currently scopes — a lone verifier still carries its own
uncorrected biases (length effects, sycophancy, prompt-injection
fragility, internal inconsistency). **If** the standalone experiment §7.3
already requires (before stage 5 is built at all) shows real catches, the
design should budget for at least two independent verifiers, not one —
which sharpens, rather than resolves, §4's "highest cost of the three"
critique of Candidate C.

### 10.7 Whole-graph narration (H12–H14): no direct hit, one caution worth
keeping in view

Multi-document summarization hallucination research shows non-trivial
error rates even in narration-only tasks (not just retrieval), with drift
concentrated away from the beginning of source material. This doesn't
invalidate the `whole_graph_stats`-plus-narration pattern's design (numbers
precomputed, narration-only) so much as confirm H13's already-documented
recurring hallucinated-count defect fits a known general pattern rather
than being a fluke specific to this graph or these models — a reason to
keep re-testing this pattern live (§9.4 already requires this), not a
reason to change the pattern itself.

### 10.8 Candidate D's core idea — precompute, don't traverse live — is
independently validated at scale, with two real caveats

GraphRAG's own architecture (Microsoft's original design — not the
FalkorDB `GraphRAG-SDK` this spike already found lacking, `q-approach2.md`'s
"Result" section) is built on exactly this idea: precompute community
summaries once, offline, so query time never re-processes raw graph
structure. "The map-reduce design avoids long-context LLM calls at query
time... this precomputation approach effectively creates materialized
views of the graph structure"
([GraphRAG: Local to Global](https://beancount.io/bean-labs/research-logs/2026/06/04/graphrag-local-to-global-query-focused-summarization), 2026). That's strong independent support for Candidate D's central
premise — real, published evidence for a pattern this spike had already
validated internally at small scale (`whole_graph_stats`, M7's golden
chain) but hadn't yet generalized to the whole catalog.

Two caveats the same research surfaces, neither fatal at our scale but
both real. First, precomputation cost is non-trivial in general —
"traversing and summarising the graph [to build the index]... adds 2-3x
higher end-to-end latency" and "the graph index... grows super-linearly
with corpus size" — worth actually *measuring* our own compile time
empirically (per §9's verification plan) rather than assuming "small
graph, therefore free": 2-3x is the *build* cost these systems accept in
exchange for cheap reads, not a number that guarantees our specific
~700-node compile is negligible. Second, knowledge-graph linearization
research warns that flattening triples into text "results in verbose text
that inflates the context window and forces the model to reconstruct
relational structure that was explicit in the original schema" — a
caution against implementing Candidate D as "dump the flat catalog as
text into the prompt." §3's design already specifies the catalog as a
queryable/filterable structure returned via a tool call (the same shape
`whole_graph_stats` already uses), not a document the model reads
wholesale — this finding confirms that was the right call, not an
incidental detail.

### 10.9 Staleness: the literature has a specific, actionable answer, and
it isn't "periodic"

A 2026 paper on eliminating stale-fact errors in agent retrieval memory
([Temporal Validity in Retrieval Memory](https://arxiv.org/pdf/2606.26511), 2026) found lazy validation — checking staleness at the moment a fact is
about to be used — outperforms proactive periodic refresh specifically for
systems where the underlying knowledge changes *unevenly*, which describes
this graph exactly: compliance data changes in bursts around reviews and
approvals, not on a clock. Separately, general RAG-freshness research
names the exact operational risk this matters for: "stale retrieval
produces confident wrong answers with no uncertainty signal"
([LLM Knowledge Base Staleness](https://atlan.com/know/llm-knowledge-base-staleness/), 2026) — the same failure shape this whole spike has already spent two
docs (`direction-correction.md`, `union-of-n.md`) trying to prevent in a
different form (a stale *Policy* or *Control* presented as current
evidence, not a stale *catalog*). §7's fix 6 adopts staleness-on-read for
this reason, replacing the user's original "periodically compile" framing
with a lazy-validation design that has independent evidence behind it,
not just internal intuition.

### 10.10 Net effect on this document

Nothing found overturns the overall synthesis in §5, and Candidate D's
addition strengthens rather than complicates it: precompute-don't-traverse
is now the single most externally-validated idea in this document, more so
than any of A/B/C individually (§10.8). What changes as a result: Candidate
D becomes the primary escalation stage for the structural core of
H1/H3/H5/H8/H9/H11, ahead of both A and B; Candidate B is deferred to a
measured, named trigger rather than built speculatively alongside D (§7
fixes 8–9); the catalog must implement staleness-on-read, not periodic
recompute (§10.9); it must be exposed as a queryable structure rather than
a linearized text dump (§10.8, already reflected in §3's design); and it
requires the same independent per-hop cross-verification that already
caught a real silent bug in v1's own history before it's trusted (§7 fix
7). Candidate B's own claimed guarantee is still narrower than §3
originally stated; the citation-completeness gate still has the
deceptive-grounding blind spot, now understood to apply to Candidate D's
output too, not only B's (§7 fix 10); and few-shot schema-grounded
examples remain a real, currently-missing lever worth testing regardless
of which stage ends up mattering most in practice.

## Next steps

1. Run the mining pass against all 39 golden Cypher queries (§7 fix 8):
   determine both Candidate D's catalog roots and Candidate B's DSL
   primitive set from the same empirical pass, not two separate
   assumptions.
2. Build Candidate D first: the catalog compiler for the mined roots,
   staleness-on-read (§7 fix 6, §10.9), and independent per-hop
   cross-verification against live FalkorDB (§7 fix 7, §10.8) — before
   anything else in this list is built.
3. Run a cheap lexical-vs-embedding entity-resolver bake-off (§10.5) on
   H3/H8/H9/H11, applied to catalog lookup, before adding an embeddings
   pipeline at all.
4. Run Candidate D against the full 39-question catalog (§9) and report,
   by name, any question it doesn't reach — this is the go/no-go gate for
   Candidate B (§7 fix 9). Do not start Candidate B before this report
   exists.
5. Only if step 4 shows a real residual gap: build the DSL compiler for
   that specific gap, with a `no_match` terminal and free-text reasoning
   permitted before constrained emission (§10.3), verified byte-for-byte
   against `SCHEMA_RELATIONSHIP_DIRECTIONS`.
6. Test schema-grounded few-shot retrieval examples (§10.1) as a
   standalone lever, the same "test before wire" discipline every other
   fix in this spike has followed — it has independent evidence behind it
   that nothing currently in this document does.
7. Extend the citation-completeness gate to both Candidate D's and (if
   built) Candidate B's output, stating its scope precisely everywhere
   it's documented (§10.2, §7 fix 10): catches missing ids, not false
   claims about correctly-cited ones.
8. Run `experiment_cross_model_verification.py` standalone against the
   already-documented H1/H11 defects, budgeting for a 2-verifier ensemble
   if it proceeds (§10.6), before deciding whether stage 5 gets built at
   all.
9. Read GraphWalker, SymAgent, and the GraphRAG precomputation literature
   (§10.4, §10.8) before building Candidate B's grammar, Candidate A/D's
   classifier and resolver, and Candidate D's compiler from scratch,
   respectively — this spike's own established discipline, not a new one.
10. Wire the fixed-order router, re-run the full 39-question catalog live,
    and report per-stage attribution — not a single aggregate score — the
    same way every prior mechanism in this spike was required to.
11. Run the live staleness test (§9, item 8) before trusting Candidate D
    in the composed router: mutate a Control's status directly in
    FalkorDB, confirm the next `ask()` call detects and recompiles rather
    than serving the stale row.
