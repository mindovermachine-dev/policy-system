# Approach 5: Interactive Scope Clarification for the Hard Tier — Detect, Don't Guess

**Scope note, same discipline as `q-approach4.md`**: this is a design document
with one embedded, live-verified measurement (§3), not a build. Nothing here
is wired into `query_mechanism_v3.py`. It sets the spec for whichever build
follows in this new `query3` spike.

## 1. Where `query2` left off

`evaluation.md` measured the router's real behavior across all 39 golden
questions:

```
v1-template:               24  (unchanged)
v2-catalog (Candidate D):   4  (H1, H5, H9, H11 — newly deterministic)
v2-agent (needs LLM):      11  (M3, M5, M14, H3, H6, H8, H10, H12, H13, H14, H15)
```

Those 11 questions are the ones this document is about. `query2` routed all
of them to `query_mechanism_v2`'s freehand agentic loop unchanged, because
`mining-pass.md` classified each as needing either free-text resolution with
an open candidate set, or "judgment beyond the joined rows." That's still the
right floor — but it was never re-examined for whether "judgment" was the
right diagnosis in every case, versus "the question just didn't say which
regulation/axis/capability it meant yet."

## 2. The idea

Don't try to make the LLM better at the 11 hard questions. Detect that a
question is in this bucket, and — instead of handing it straight to the
freehand agent — run one short, structured clarification step that asks the
user for whatever specific piece of scope the question is missing, using
real values pulled from the graph (loaded regulation ids, resolved capability
candidates, real status fields), then re-enter the router with the
now-scoped question. Only fall through to the freehand agent (or an explicit
refusal) for whatever's left over once a question is actually, unavoidably,
open-ended.

This is the same "eliminate, don't reduce the rate of" standard `q-approach4.md`
§2 set for itself, aimed at a different part of the pipeline: instead of
constraining *how* the model answers, constrain *what's being asked* before
it gets a chance to guess.

## 3. What's actually judgment, and what's just a missing parameter — measured, not assumed

The user brought a general customer-facing GRC framework for handling broad
questions (scope clarification → overview → obligations → assumptions →
narrowed answer → table → next steps → follow-up). Before adopting its shape,
it's worth checking whether the 11 questions it would apply to are actually
open-ended the way `mining-pass.md` said, or whether some of them just need
one more fact the graph already has. Three of the eleven were checked live
against `policy_system` (FalkorDB) while writing this document:

**M14** ("Which of our draft Policies are blocking GDPR readiness?")
`mining-pass.md` classified this as needing judgment because "no edge
encodes GDPR-relevant." Checked directly:

```
MATCH (reg:Regulation)-[:DEFINES]->(:Role)-[:HAS]->(o:Obligation)-[:REQUIRES]->
      (c:Capability {id:$cap})
RETURN DISTINCT reg.id, o.id
```

- `cap_data_protection_impact_assessment_a51acb` → **10 obligations, all
  `GDPR-1.0`**.
- `cap_clinical_trial_data_integrity_f28d55` → 2 obligations, both
  `HELVEX-SOP-*` — **zero `GDPR-1.0`**.

"GDPR-relevant" *is* structurally encoded — it's just encoded one hop further
back than the Policy→Capability edge the original framing looked at. The
draft Policy governing the DPIA capability is GDPR-blocking; the draft Policy
governing Clinical Trial Data Integrity is not, and a mechanism that reported
both (or asked an LLM to guess) would risk exactly the golden rubric's stated
failure mode ("conflate this with the separately-stale... Policy"). The real
gap wasn't judgment. It was that the question named a regulation ("GDPR")
that the original Candidate D catalog schema doesn't carry as a filterable
column all the way from Policy.

**H6** ("If we adopt a 'Software Bill of Materials' capability, would it be
redundant?") Same check:

```
SBOM -> [('CRA-1.0', 'obl_identify_and_document_components_via_software_bill_of_materials_dcfaae')]
```

One row, one regulation. "Would it be redundant [for NIS2/GDPR]" is a filter
on that same result set — `count(rows WHERE reg.id <> 'CRA-1.0') == 0` — not
a semantic call about what "redundant" means. The golden rubric's own
required answer ("correctly report zero current redundant coverage") is
exactly that count.

**M3** (security-logging coverage across all three regulations) — not
independently re-queried here since `golden-answers.md` already gives the
per-regulation breakdown directly (CRA + Helvex only, confirmed), but it's
the same shape as H6: a reverse walk from one capability, grouped by
regulation, where the "judgment" the rubric asks for is "state the absence
explicitly, don't omit it" — a formatting requirement on a `GROUP BY`, not a
reasoning step.

**What this means:** three of `mining-pass.md`'s eleven "needs an LLM"
classifications were judgment-by-default because the question was
*underspecified* (which regulation? redundant against what?), not because
the graph lacks the fact. Once the missing parameter is supplied — by
*asking*, not by an LLM guessing it — the answer is exactly the kind of
reverse-walk-plus-filter Candidate D's catalog already computes for H1/H5/H9/H11.
This is a real, measured correction to `mining-pass.md`, not a hypothesis
being carried forward unverified — the same standard `q-approach4.md` §7
fix 1 set for itself.

## 4. Full reclassification of the 11

| Q | `mining-pass.md`'s diagnosis | Reclassified here | Missing piece, if scope-ambiguous |
|---|---|---|---|
| M3 | judgment (absence claim) | **scope-ambiguous** → deterministic once decomposed | none — already fully scoped (capability + "all 3 regs"); the "judgment" was a narration requirement, not a missing parameter. Route to catalog with an explicit "state absent regs" formatter, no clarification round-trip needed. |
| M14 | judgment (GDPR-relevance) | **scope-ambiguous** → deterministic once decomposed | which regulation ("GDPR readiness" names it, but the catalog needs to *carry* Regulation as a column reachable from Policy, which it doesn't yet — a catalog-schema gap, not a query-mechanism one) |
| H6 | judgment (redundancy) | **scope-ambiguous** → deterministic once decomposed | which regulation(s) "redundant" is measured against (already implicit in "if we adopt," i.e. "for the regulations that don't already require it" — resolvable without asking, same as M3) |
| H3 | free-text scenario, 2 capabilities | **scope-ambiguous** → deterministic once decomposed | which capability/capabilities the scenario touches — genuinely needs a pick, since "logs access but doesn't encrypt" names two distinct claims a resolver can't safely split unattended |
| M5 | judgment (semantic role comparison) | **stays judgment** | nothing to ask for — confirmed live in `golden-answers.md`: CRA's 6 roles and NIS2's 2 roles share no vocabulary; "similar" has no structural referent in the graph at all |
| H8 | open candidate set | **stays resolver-shape**, not judgment (mining-pass.md's own conclusion, unchanged) | not a single missing parameter — genuinely open ("what capabilities should I think about"); best fit is a guided multi-select over top-k candidates, not a single clarifying question |
| H10, H15 | schema gap | **stays schema gap** | nothing to ask for — no clarification produces an answer the graph structurally cannot give |
| H12, H13, H14 | open narration | **partially scope-ambiguous** | prioritization axis (deadline / implementation-status / draft-vs-approved — all real columns) turns "what should we prioritize" into a deterministic sort; the final prose wrapping (H13 specifically) stays a narration step, unchanged from `evaluation.md`'s already-validated `whole_graph_stats` + narration pattern |

Net: of the 11, **4 collapse to fully deterministic once the router asks one
concrete question (M3, M14, H6, H3)**; **3 partially do (H12–H14, structural
ranking yes, final prose no)**; **1 is a genuine resolver-shape gap, not
judgment (H8)**; **1 is genuinely irreducible (M5)**; **2 are schema gaps
unaffected by any of this (H10, H15)**.

## 5. What transfers from the pasted framework, and what doesn't

The framework the user brought (scope clarification → landscape overview →
obligations → assumptions → narrowed context → table → next steps →
follow-up) is written for a general GRC/consulting conversation, not for this
specific graph. Checked against what's actually in the schema:

**Transfers directly, re-grounded in real graph values instead of invented
categories:**
- *Impose structure before asking for detail* (its §1) — matches §4's
  approach exactly: don't ask "tell me more," ask "which of these five loaded
  regulations?" with the real `Regulation.id` values as the choices.
- *Surface assumptions explicitly* (its §4) — matches H3's resolved-capability
  confirmation step ("I read your scenario as touching
  `cap_data_encryption_0e50d3` and `cap_access_control_authentication_151816`
  — is that right?") before running the deterministic chain.
- *Structured summary table* (its §6) — this is exactly the shape M5's answer
  should take: the two real role sets side by side, not a synthesized
  verdict.
- *Concrete next steps* (its §7) — maps to H14 once a prioritization axis is
  picked: a deterministic, sorted punch list, same items `golden-answers.md`
  already names (the `planned` Vulnerability Patch SLA Check, the overdue
  Incident Triage review, etc.), not generic advice.

**Does not transfer, and adopting it verbatim would be a mistake:** the
framework's own worked examples — "CRA product classes," "conformity route,"
"Annex III classification," "control mapping" as a distinct step — describe
concepts this graph's schema does not contain. There is no `Product` or
`System` node (that's `H10`'s exact, already-documented schema gap) and no
`Annex`/classification property anywhere in `ps-domain-concepts.md`'s eight
node labels. A clarification flow that offers "product classification" as a
guided link would be promising an answer the system cannot structurally
deliver — the same failure category `golden-answers.md` H10/H15 already
named ("not a query-mechanism gap, a missing concept in the domain model").
The framework's *shape* is reusable; its *content* has to be re-authored
per-question against this graph's real columns, never borrowed wholesale.

## 6. Proposed design

```
question
  │
  ▼
[1] v1 template router (unchanged)  ──── match ──→ answer
  │ NO_TEMPLATE_MATCH
  ▼
[2] Candidate D catalog lookup (unchanged, per query2)  ──── match ──→ answer
  │ no catalog root covers this question as asked
  ▼
[3] NEW: hard-tier shape matcher — structural, not an LLM call.
    Checks the question against the 11-row table in §4 by the same
    signal mining-pass.md already used (entity-resolver hit / miss,
    presence of a scenario clause, presence of an aggregate-without-axis
    shape) — not free classification.
  │
  ├─ matches a "scope-ambiguous" row (M3, M14, H6, H3, H12–H14's ranking)
  │    ▼
  │  [4] Guided clarification — closed-choice only, values pulled live
  │      from the graph (Regulation.id list, top-k resolved Capability
  │      candidates, sortable Control/Standard/Policy status columns).
  │      Never free text at this step — free text is what made the
  │      question ambiguous in the first place.
  │    ▼
  │  [5] Re-enter router at [1]/[2] with the scoped question.
  │      For H12–H14: deterministic ranked result; optional narration
  │      pass only for H13's prose framing, same pattern as
  │      whole_graph_stats + narration, unchanged.
  │
  ├─ matches "resolver-shape, open set" (H8)
  │    ▼
  │  guided multi-select over top-k resolved candidates, not a single
  │  clarifying question — user confirms/deselects, result set returned
  │  directly, no narrative verdict claimed
  │
  ├─ matches "genuine judgment, no structural referent" (M5)
  │    ▼
  │  present the real, structured comparison (§5's table) and stop —
  │  the system's answer *is* the table, not a similarity verdict it
  │  isn't positioned to make
  │
  ├─ matches "schema gap" (H10, H15)
  │    ▼
  │  refuse directly, name the missing concept — unchanged from
  │  query1/query2's existing discipline, no clarification offered
  │  because none would help
  │
  ▼ no match at [3] at all
[6] v2's existing freehand agentic loop, unchanged — floor of last resort
```

## 7. Critique of this design

1. **The reclassification in §3 was checked on 2 of 11 questions live, not
   all 11.** M3 was checked by re-reading `golden-answers.md`'s existing
   entry, not independently re-queried. H12–H14's "axis" claim and H3's
   "2-capability pick" claim are design-level reasoning, not yet run against
   FalkorDB the way M14/H6 were. Before this design is built, every row in
   §4's table needs the same live check M14 and H6 got — the same
   "measure, don't assume" bar `q-approach4.md` and `mining-pass.md` already
   held themselves to, not relaxed here just because two checks came back
   clean.
2. **Stage [3]'s matcher has to be genuinely structural, or this design
   quietly reintroduces the problem it's built to avoid.** If "does this
   question need clarification" itself requires an LLM call to decide, the
   freehand judgment step this document is trying to move *later* in the
   pipeline just moved *earlier* instead — a classifier making the same kind
   of unconstrained call `q-approach4.md`'s failure class #7
   (NL-to-Capability mapping "done ad hoc... exactly the kind of
   unconstrained inference that produced failures") was written against.
   The matcher must be keyed off the same closed, mined shape table in §4 —
   regex/entity-resolver-miss signals, not free classification — the same
   discipline Candidate A's classifier was held to in `q-approach4.md` §3.
3. **This adds a round-trip cost the current design doesn't have**, and
   that cost isn't uniformly worth it. For M3/M14/H6/H3, the clarification
   step replaces a slow, sometimes-wrong freehand agent call with a fast
   deterministic one — a clear win, the same shape of win `evaluation.md`
   §6 already measured for H1/H11. For H13 ("one paragraph for the board"),
   asking "which axis?" before answering may be the wrong shape entirely —
   a board-summary question is plausibly one where the user wants a single
   best-effort synthesis, not a wizard. This needs to be tested with real
   users or at minimum a rubric check on whether the clarified answer
   scores meaningfully better than `v2`'s unmodified attempt, not assumed
   because the mechanism is cleaner.
4. **§5's "doesn't transfer" list is a design decision, not yet a tested
   guardrail.** Nothing here yet enforces, in code, that the guided-choice
   step can only ever offer options that resolve to real graph values — that
   has to be a hard constraint on whatever builds stage [4], not a
   convention that could silently drift once someone adds a "which product
   type" option because the pasted framework suggested it.
5. **M5's "present the table and stop" is a real answer, not a partial
   one, but that has to be stated to the user, not implied.** A mechanism
   that returns two role-name lists side by side without a verdict needs to
   say explicitly *why* — "the graph doesn't encode role equivalence across
   regulations, here are both sets" — matching the same honesty discipline
   `is_current_evidence` already established for staleness, extended here to
   "we deliberately aren't the ones who judge similarity."

## 8. Next steps — all executed, see §9 for results

1. ✅ Independently verify the remaining 9 rows of §4's table live against
   FalkorDB (M3 fully, H3's 2-capability split, H12–H14's axis claim, H8's
   top-k shape) — the same discipline M14/H6 already got in this document,
   not carried forward as an assumption. → `verify_remaining_rows.py`, §9.1.
2. ✅ Extend Candidate D's catalog schema (`catalog.py`) with a `Regulation`
   column reachable from `Policy` — **turned out unnecessary**; the live
   check found the column already exists by construction. → §9.2.
3. ✅ Specify stage [3]'s matcher as a closed rule table (mirroring §4), and
   build it before anything downstream. → `clarifier.py`, §9.3.
4. ✅ Prototype the guided-clarification UI shape for H3 and run it live
   before/after against `v2`'s unmodified agent. → `experiment_h3_before_after.py`, §9.4.
5. ✅ Test whether H13/H14 actually benefit from an axis-selection step or
   whether users prefer a single best-effort synthesis. → `experiment_axis_selection.py`,
   §9.5 — **mixed result, not a clean win**, see below.

## 9. Results — all five next steps executed, against the live graph and real local models

Same discipline `evaluation.md` held `query2`'s Candidate D to: every claim
below points at a script in this directory, every script is re-runnable
(`python3 <file>.py`), and negative/mixed results are reported as such, not
smoothed over.

### 9.1 Live verification of §4's table (`verify_remaining_rows.py`)

All five previously-unverified rows checked directly against `policy_system`:

- **M3**: `cap_security_logging_c4d9e2`'s requiring obligations come from
  `{CRA-1.0, HELVEX-SOP-1.0}` only — confirms the golden scope exactly, no
  NIS2/GDPR coverage. §4's classification stands.
- **H3**: both scenario clauses resolve to their expected capabilities as
  the resolver's top-1 hit — `'logs access'` →
  `cap_access_control_authentication_151816`, `"doesn't encrypt data at
  rest"` → `cap_data_encryption_0e50d3`. Both currently sit under the same
  approved Policy with an implemented Control — meaning the graph cannot,
  on its own, tell "the org has this control" apart from "this specific
  endpoint implements it." That's not a gap in this design; it's a real
  boundary — see 9.4's `answer_h3_scenario` for how the design handles it
  (the endpoint's own pass/fail claim is the one input this mechanism does
  not try to derive from the graph).
- **H12–H14**: every number `golden-answers.md`'s H12/H13 rubrics cite was
  reproduced exactly from catalog columns — 68 capabilities (13 governed /
  55 ungoverned), 4 Policies (2 approved / 1 draft / 1 deprecated), 6
  Controls (4 implemented / 1 planned / 1 deprecated), 1 overdue Control.
  Three real, sortable axes exist as actual graph columns:
  `Control.next_review_date`, Policy/Standard approval state, and
  Capability governed-vs-ungoverned. No "business criticality" column
  exists anywhere in the schema — confirming §5's warning that an axis
  picker must only offer axes the graph actually has.
- **H8**: even broadened multi-term free-text queries (`"stores customer
  PII"`, `"PII"`, `"personal data storage compliance"`, `"customer personal
  data database"`) at `top_k=10` reach at most 2 of the 5 golden
  capabilities. Confirms `mining-pass.md`'s and `evaluation.md`'s existing
  conclusion: H8 is a genuine resolver-shape gap (needs guided multi-select
  across distinct compliance dimensions), not reachable by widening a
  single free-text call regardless of `k`.

### 9.2 Catalog schema: the predicted gap didn't exist (`catalog_answers_v4.py`)

§4 originally predicted M14 needed a new `Regulation` column reachable from
`Policy`. Checked directly: `compile_catalog()` already joins the *entire*
chain (`Regulation→Role→Obligation→Capability→Policy→Standard→Control`) in
one pass, so every row already carries `regulation_id` and `policy_id`
together. Filtering `[r for r in catalog.rows if r.policy_id == X]` and
collecting `{r.regulation_id for r in ...}` already answers "which
regulations does this draft Policy's capability serve" with zero schema
changes — confirmed against `pol_clinical_data_integrity_policy_e1a539`,
which resolves to `{GDPR-1.0}` for its DPIA capability and
`{HELVEX-SOP-1.0, HELVEX-SOP-2.0}` for its Clinical Trial Data Integrity
capability, matching `golden-answers.md`'s M14 rubric exactly (name the
Policy specifically; recognize the DPIA capability, not the other one, as
the real GDPR-relevant driver). This is itself a finding worth stating
plainly: §4's own prediction was wrong, caught by checking rather than
building the predicted fix first.

`catalog_answers_v4.py` implements and live-verifies four deterministic
answer functions against their real rubrics:

| Function | Question | Verified against |
|---|---|---|
| `answer_m3_capability_coverage` | M3 | Explicit per-regulation coverage/absence, matches golden scope |
| `answer_m14_draft_policies_blocking` | M14 | Names the real Policy, correctly flags only the GDPR-relevant capability as blocking |
| `answer_h6_redundancy` | H6 | Matches golden exactly: SBOM required only by CRA, "no redundant coverage today" |
| `answer_h3_scenario` | H3 | See 9.4 |
| `answer_h12_14_prioritized` | H12–H14 | See 9.5 |

### 9.3 The hard-tier matcher (`clarifier.py`)

A closed rule table, not a classifier — every one of the 11 questions is
matched by a specific regex against a specific known shape, per §7 critique
point 2's requirement. Run against all 11:

```
M3    kind=direct            (answers immediately -- regulation/capability already named in the question)
M14   kind=direct            (regulation already named: "GDPR readiness")
H6    kind=direct            (capability already quoted in the question)
H3    kind=clarify           (pre-fills a default clause split, shown for confirmation, never auto-trusted silently)
M5    kind=present_and_stop  (returns the real CRA/NIS2 role sets, no verdict)
H8    kind=clarify           (guided multi-select across 5 real compliance dimensions)
H12   kind=clarify           (axis pick)
H13   kind=clarify           (axis pick)
H14   kind=clarify           (axis pick)
H10   kind=refuse            (schema gap, named directly)
H15   kind=refuse            (schema gap, named directly)
```

Wired into `query_mechanism_v4.py` as stage 3 between the unmodified
`v1`+catalog stages and `v2`'s freehand fallback. Full 39-question
attribution, re-run through the new router:

```
v1-template:            24  (unchanged)
v2-catalog:               4  (H1, H5, H9, H11 -- unchanged from query2)
v3-clarified:              3  (M3, M14, H6 -- newly deterministic, zero round-trip)
v3-table-only:             1  (M5 -- real data, no verdict claimed)
v3-needs-clarification:    5  (H3, H8, H12, H13, H14 -- await one closed-choice answer)
v3-refuse:                 2  (H10, H15 -- unchanged)
```

No mismatches against §4's predicted classification. The number that
matters most: of `query2`'s 11 LLM-routed questions, only **5** still need
any interaction at all, and of those, **4** (H3, H8, H12, H14) resolve to a
fully deterministic answer immediately after one closed-choice clarification
— no LLM call anywhere in their path. Only H13 keeps an open question about
whether an LLM step belongs in its answer at all (9.5).

### 9.4 H3 live before/after (`experiment_h3_before_after.py`)

Real model (`qwen3-coder-next:q4_K_M`), real graph, same question text,
`v2`'s unmodified freehand agent vs. the new clarify-then-answer path:

| | Before (`v2` freehand, single run) | After (clarify + `answer_h3_scenario`) |
|---|---|---|
| Result | **Failed to converge** — no final answer after 8 turns (20.2s) | Correct, specific verdict (0.0025s) |
| Cites `cap_data_encryption_0e50d3` | N/A (no answer) | Yes |
| Concludes NON-COMPLIANT specifically | N/A | Yes |
| Explicit NL→Capability mapping | N/A | Yes, both capabilities named |

The "before" failure reproduces the exact non-convergence class
`union-of-n.md` already documented for H1 with this same model — not a new
finding, but a second live confirmation on a different question. The
"after" path passes all three rubric checks with zero LLM calls, using the
clarifier's pre-filled default clause split (verified in 9.1 to resolve
correctly) auto-confirmed rather than hand-edited, since this run had no
human in the loop — a real deployment would show the two lines in
`clarifier.py`'s `_match_h3` output to the user before computing the
verdict, per the design's own "never auto-trust a parsed guess silently"
principle (§7 critique point 1's caution, addressed directly here).

### 9.5 H13/H14 axis-selection test (`experiment_axis_selection.py`) — the one real negative/mixed result

This is the test §7 critique point 3 flagged as having neither live
evidence nor precedent — and it came back mixed, not a clean win for the
new design:

**H14 ("what should we prioritize"): axis-selection wins, and catches a
real omission.** `v2`'s unmodified agent (8.8s, live) produced a plausible,
well-formatted answer — but silently **dropped the `planned` Vulnerability
Patch SLA Check control** (`ctrl_std_pol_incident_vulnerability_response_
policy_9de859_v2_automated`), a specific item `golden-answers.md`'s H14
rubric requires by name. This is the same shape of defect `evaluation.md`
§6 found for H11 ("citations complete, one specific required item still
dropped") — under-citing that survives even a coherent, confident-sounding
answer. The axis-clarified deterministic version (`review_urgency` axis)
names it directly, in 0.002s, because it's reading the same `planned`
status column the freehand agent had access to via `run_cypher` but didn't
surface in its final prose.

**H13 ("one paragraph for the board"): axis-selection is the wrong shape.**
`v2`'s unmodified agent actually did well this run (3.8s, live) — a
coherent single paragraph hitting all four rubric numbers (68/13/55,
overdue count). The axis-clarified alternative produces three separate
itemized lists, not a paragraph; a union of the three technically contains
every required fact (confirmed by rubric substring check) but does not
satisfy what H13 actually asked for — a synthesized narrative a person
reads once, not three raw tables. This is exactly the risk §7 critique
point 3 named before running anything: for a "give me a synthesis"
question, decomposition into closed choices may be solving a problem the
user didn't have.

**This does not mean H13's single-shot synthesis is reliably safe** — one
passing run here doesn't override `q-approach4.md`'s own prior finding of
H13 "fabricated control counts, twice, in two separate re-runs, on
different specific numbers each time." The right reading of this pair of
results, taken together, is a **design correction**, not a wash: H14
genuinely benefits from being decomposed into a ranked/itemized shape
(prioritization naturally wants a list); H13 should **not** be decomposed
the same way — instead, the axis-computed facts (already deterministic and
already verified in 9.1/9.2) should be handed to a single narration call as
grounding context, the same `whole_graph_stats`-plus-narration pattern
`evaluation.md` already validated for H12–H14 in `query2`, rather than
replaced by raw punch lists. `q-approach5.md`'s §4/§6 design is revised
accordingly: H14 routes through the new axis-clarified deterministic path;
H13 keeps a narration step, now grounded in the same deterministic facts
instead of a fresh, ungrounded `run_cypher` exploration — a smaller, more
targeted change than either "clarify everything" or "clarify nothing."

### 9.6 Net effect

Of `query2`'s 11 LLM-routed questions: **7 now resolve with zero LLM calls
at any point** (M3, M14, H6 immediately; H3, H8, H12, H14 after one
closed-choice clarification) — up from 4 in `query2`. **1 correctly stops
at a structured comparison table with no verdict claimed** (M5) — unchanged
in spirit from `query2`, now made explicit rather than silently deferred to
`v2`. **1 keeps a narration step but now grounded in pre-verified
deterministic facts rather than fresh exploration** (H13, revised per 9.5).
**2 are unaffected, schema gaps correctly refused** (H10, H15). This
matches this document's own §2 framing exactly: not "make the LLM better,"
but "shrink what's actually left for it to do, and be honest about the one
place a live test showed shrinking it further didn't help."

## 10. Generalization stress test: does this handle vague questions it wasn't tuned on?

§9's results are real but earned entirely on the 11 questions `clarifier.py`
was built and tuned against — every matcher in it is a regex written
directly from those 11 literal phrasings. That's exactly the overfitting
risk worth checking before trusting §9.6's numbers as a general claim
rather than a fixed-catalog one. `experiment_generalization_stress_test.py`
runs 20 new questions, none from `golden-answers.md`, built to probe this
directly: close paraphrases of the 11 known shapes (does the matcher
generalize at all?), genuinely novel global questions with no shape built
for them (does the router degrade safely?), and phrasings chosen to risk a
false-positive match (a confident wrong answer is worse than an honest
fallthrough). Each question's expected outcome was written down *before*
running the script, so a mismatch is a real finding, not a rationalized one.

### 10.1 Result: overfitting confirmed, directly

```
v2-agent (would need LLM):  19 / 20
v3-clarify:                  1 / 20
```

Only **N7** ("Where are we most exposed right now?") matched — and
correctly: it contains the literal substring `"most exposed"` the H12–H14
matcher was keyed on, and the resulting axis-pick clarification is exactly
right for that question despite it being far shorter than H12's original
phrasing. Every other paraphrase — including direct rewordings of M14
("which policies still sitting in draft status could delay our GDPR
readiness" — same question as M14, different verb structure), H6, H3, M5,
H8, H12, H13, H14, H10, H15, and even query2's own already-shipped H5
catalog template ("might be stale given how regulations have shifted
recently" vs. its regex's `"potentially out of date"`) — fell through
entirely unmatched. **Zero false positives** — nothing in `clarifier.py` or
`query_mechanism_v3.py`'s existing `CATALOG_TEMPLATES` misfired with a wrong
confident answer on any of the 20, including the ones deliberately worded
to look close to a trigger phrase without being the same shape (N2, N19).
The matcher's failure mode is safe (fall through) rather than dangerous
(silently wrong) — but it is a real, near-total failure to generalize
beyond the literal training phrasing, not a partial one.

### 10.2 The bigger problem this surfaced: the fallback itself isn't reliable either

§9's design leans on stage 4 (`v2`'s unmodified freehand agent) as "the
fallback of last resort — never worse than what query1 already has." Four
of the 19 fallthroughs were spot-checked live
(`qwen3-coder-next:q4_K_M`, single run, real graph) to see whether "falls
through safely" also means "still gets a usable answer":

| # | Question | Result |
|---|---|---|
| N1 | "Which policies still sitting in draft status could delay our GDPR readiness?" (= M14, reworded) | **Failed to converge** — no answer after 8 turns, 19.0s |
| N14 | "Can you give our compliance program a maturity score out of 10?" | Good — explicitly refuses to fabricate a score, correctly states no scoring mechanism exists, offers real deterministic alternatives |
| N15 | "What obligations do we have specifically around AI systems?" | **Failed to converge** — no answer after 8 turns, 10.6s |
| N18 | "What's standing between us and full NIS2 compliance?" | **Failed to converge** — no answer after 8 turns, 13.0s |

**3 of 4 spot-checked fallthroughs failed to converge at all** — not a wrong
answer, no answer. This is the same non-convergence class `union-of-n.md`
already documented for this model on H1, now confirmed on three previously
untested questions, including N1 — a *plain rewording of M14*, a question
this design answers deterministically in 0.002s when phrased the way
`golden-answers.md` phrases it, and that the freehand agent cannot answer
at all when phrased the way an actual user just as plausibly would.

### 10.3 What this means for §9's claims

§9.6 said 7 of `query2`'s 11 LLM-routed questions "now resolve with zero LLM
calls at any point." That statement is **correct as measured, and
narrower than it reads** — it's true for those exact 11 questions, not for
"hard global questions" as a class. This stress test's honest bottom line:

- The deterministic gains in §9 (M3, M14, H6, H3, H8, H12, H14) are real,
  live-verified, and worth keeping — nothing here undoes them.
- They do not yet generalize past close variants of their exact tuning
  phrasing. A user who asks the *same underlying question* about M14 in
  different words gets routed to a fallback that, in this sample, fails to
  converge 75% of the time — a materially worse outcome than either the
  deterministic path or, arguably, than not building this stage at all for
  that specific phrasing (since the fallback was already there in `query2`
  and carries the same risk on its own).
- The router's own safety property held (no false positives), which matters
  — but "fails safe" isn't the same claim as "handles the question," and
  this document shouldn't let the first stand in for the second.

### 10.4 Fixing this is a real design fork, not a small patch

Three directions, not adopted here, presented for a decision rather than
picked unilaterally:

1. **Broaden the regexes** (more keyword variants, looser anchoring). Cheap,
   but has a ceiling — N1 through N20 alone suggest the space of real
   phrasings is larger than any hand-enumerated pattern set will cover, and
   every broadening pass risks trading false negatives (safe) for false
   positives (dangerous) the more permissive it gets.
2. **A lightweight structural/keyword classifier** scored against each
   shape's known vocabulary (e.g. "draft," "blocking," "readiness" as a
   weighted set for M14, not an exact phrase) rather than a single regex —
   more permissive than 1, still not a free LLM call, but needs its own
   false-positive testing before trusting it, per §7 critique point 2's
   standing requirement that stage 3 stay structural.
3. **Accept the narrower scope and say so** — ship this design explicitly
   as "improves the 11 questions this graph's own golden catalog names, not
   a general hard-question detector," and separately invest in making
   stage 4's freehand fallback itself converge more reliably (e.g. the
   union-of-n resampling `query_mechanism_v2` already has, currently run at
   `union_runs=1` in these experiments to keep them fast) — since §10.2
   shows that's now the more consequential weak point for anything outside
   the 11.

Not resolved in this document — the right call depends on how much
engineering investment the next phase of `query3` is meant to get, which is
the user's call, not an inference to make here.
