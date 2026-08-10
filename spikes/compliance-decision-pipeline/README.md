<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: Compliance Decision Pipeline (Answer Verification)

**Status:** Proposed 2026-08-09, not yet implemented or run. Design derived
in-session from `cli-tool-semantics` grading; see "What We Already Know"
below for the retroactive analysis that shaped it — already done, no new
runs required to get this far.

## Purpose

Test whether a question can be **routed by term-coverage and structural
shape** (not by author-assigned difficulty tier), verified against
**independently re-derived graph evidence** (not a hand-authored golden
answer), and returned as an **evidence-annotated answer** rather than an
autonomous compliance verdict — and whether that combination reliably
separates questions the system can answer with confidence from questions it
should decompose, show evidence for, or refuse.

This tests [AD-7](../../docs/architecture/ps-prototype-architecture.md)
(the Answer Verification Pipeline component).

## Design History (why this isn't the first draft)

This spike's design changed twice during scoping, and both changes matter
enough to keep on record:

1. **Autonomous decision → evidence-annotated answer.** The initial framing
   had an LLM judge arbitrate a compliance verdict. That doesn't escape the
   non-determinism problem this whole line of work started from — a
   single-shot judge is exactly as unreliable as a single-shot answer. The
   design now defaults to *showing the evidence* wherever verification can't
   clear a high bar, and reserves the judge for a narrow residual, run as an
   ensemble rather than trusted single-shot.
2. **Golden-answer fitness function → graph-derived fitness function.**
   Checking an answer against `dev-answers.md` inherits that document's own
   defects (confirmed twice already — see `cli-tool-semantics/RUNBOOK.md`'s
   LC-H2 and RM-H1 findings). Where a question type has a canonical,
   trusted query shape, the fitness check re-derives ground truth from the
   graph directly instead of trusting a golden text answer.
3. **Tier-based routing → failure-kind-based routing.** The original plan
   routed "Hard-tier questions get decomposed or refused." Retroactive
   analysis (below) showed this doesn't work — tier doesn't predict
   reliability. What predicts it is term-coverage and structural shape.

## What We Already Know

### The tier hypothesis, tested and rejected

Recomputing pass rate by tier (E/M/H, already encoded in the question IDs)
across the two existing graded runs:

| Tier | `cli-tool-semantics` dev-v1 | `cli-tool-semantics` dev-v2b | `skill-transfer` held-out |
|---|---|---|---|
| Easy | 16/18 — 88.9% | 16/18 — 88.9% | 17/18 — 94.4% |
| Medium | 15/18 — 83.3% | 16/18 — 88.9% | **12/18 — 66.7%** |
| Hard | 12/18 — 66.7% | 10/18 — 55.6% | 15/18 — 83.3% |

`cli-tool-semantics` shows a clean E>M>H gradient in both its runs.
`skill-transfer`'s held-out run inverts it — Medium is worst, not Hard.
Tier does not transfer as a reliability predictor across datasets. Do not
route on it.

### The failure-kind tally that replaces it

Reclassifying all 33 failure instances across the three runs by *what kind
of mistake it was*, instead of by tier:

| Failure kind | dev-v1 | dev-v2b | held-out | Total |
|---|---|---|---|---|
| **Completeness** (dropped caveat/ID/option, under-argued) | 7 | 8 | 1 | 16 |
| **Query-construction** (search/filter under-scoped, real data not surfaced; zero-rows misread) | 0 | 3 | 2 | 5 |
| **Miscount** (arithmetic wrong despite correct underlying data) | 2 | 1 | 0 | 3 |
| **Definitional/boundary** (defensible non-golden definition of a term) | 0 | 0 | 3 | 3 |
| **Over-claiming** (scope stated broader than supported) | 0 | 0 | 2 | 2 |
| **Granularity-slip** (right numbers, wrong unit/bucket) | 0 | 0 | 2 | 2 |
| **Wrong Format** (correct substance, unusable structure) | 0 | 0 | 0 | 0 — literature category, zero confirmed instances so far, watched for in Stage 5 |

Two patterns, both load-bearing for this spike's design:

- **Miscount is CLI-path-specific** — zero instances across skill-transfer's
  two raw-Cypher runs (dev *and* held-out), 3 across the CLI runs. Raw
  Cypher's `count()` does the arithmetic; the CLI's raw-row JSON pushes
  counting onto the agent. An implementation gap, not a ceiling —
  dev-v2b's `row_count` field already partially fixed it.
- **Definitional/boundary, over-claiming, and granularity-slip are
  held-out-exclusive** — 7 of 10 held-out failures, zero across both
  `cli-tool-semantics` runs, despite those runs using the *same* question
  design (same 9 groups, same tier structure) drawn from the dev pool
  instead. Reads as a novelty effect, not a tool-surface or tier effect:
  these are questions that hit a term the skill's Canonical Definitions
  section doesn't cover yet. Dev-pool boundary terms got hardened through
  prior iteration; held-out-pool ones hadn't been tested at all before
  grading.

A concrete example the term-coverage stage (below) is built to catch:
dev-v2b's AU-H2 note states a live control with a lapsed review is
"overdue," not "stale," per the skill's own canonical definitions — a real
instance of two domain terms that sound like synonyms but are deliberately
distinct. A naive synonym match would merge them and miss exactly the
boundary this class of failure lives on.

### Cross-checked against external research

This taxonomy was derived entirely from our own 33 failures. A pass through
the broader RAG/QA-evaluation literature (full notes and sources in
[RESEARCH.md](./RESEARCH.md)) found Completeness and Granularity-slip match
two of exactly seven canonical failure points in Barnett et al.'s
independent three-case-study RAG production research, and Miscount matches
a named category in the hallucination-taxonomy literature — convergent
validation from work with no connection to this project. It also
surfaced two adjustments folded in above and below: Query-construction's
definition is sharpened to Barnett's specific "relevant data exists but the
search/filter is under-scoped" shape, and **Wrong Format** — correct
substance, unusable structure — is added as a named literature category
with zero confirmed instances so far, watched for going forward rather than
retrofitted onto existing data. One thing Barnett's framework flags that
does *not* apply to us: "Not in Context" (retrieved data lost to
context-window truncation) has no analog here, since the CLI returns direct
structured JSON from deterministic queries rather than ranked chunks
assembled into a context window — a structural side-effect of AD-3, not
something this pipeline needs to guard against separately.

### Failure kind → catching mechanism (not a 1:1 with "definitional")

Walking each failure kind through the design step by step surfaces that
"definitional/boundary" is not one problem with one fix — it splits into
two, and two of the other kinds aren't predictable before an answer exists
at all. This matters because it changes what Stage 1 can honestly promise:

| Failure kind | What actually catches it | When |
|---|---|---|
| Miscount | Stage 2 structural check (count-shaped → tool-computed number) | Pre-answer (routing) |
| Completeness | Stage 2 (multi-part → decompose) + composed-answer fitness gate | Pre-answer + post-answer |
| Definitional/boundary — undefined **vocabulary** (e.g. AU-M4's stale over-extension) | Stage 1 alias table, no-match | Pre-answer (routing) |
| Definitional/boundary — undefined **interaction** between two defined terms (e.g. SEC-M2/SEC-M4: does "overdue" implicitly exclude "deprecated") | Stage 4 rule check — Stage 1 cannot see this, both terms are individually defined | Post-answer only |
| Query-construction | Stage 4 independent re-query | Post-answer only |
| Over-claiming (e.g. AU-H4: true fact, over-extended scope) | Stage 4 evidence grounding — **scope match**, not plain existence | Post-answer only |
| Granularity-slip (e.g. EM-E3: answered in "controls," question asked "chains") | Stage 1 records the question's entity-type; Stage 4 cross-checks the answer's stated unit against it | Pre-answer capture + post-answer check |

Three of six kinds have no pre-answer signal at all — they're properties of
*how an answer was produced*, not of the question text. Stage 4 is not a
thin backstop behind Stages 1–2; it's the only place roughly half these
failure kinds can be caught, no matter how good the pre-answer classifier
gets.

### Other prior findings this design reuses directly

- `DEV-V2B-KICKOFF.md` already found `ps-domain/SKILL.md` contains written
  rules for several failure classes (rule 3: account for every row
  returned; rule 8: unit-of-counting discipline; rule 5: cite real IDs)
  that the agent violated anyway — evidence for a missing *enforcement*
  step, not a missing instruction. That's what Stage 4's rule checks are.
- AD-2 already establishes "deterministic inside the boundary, LLM
  judgment outside it." This pipeline applies that same split one stage
  further downstream, to the answer itself.
- The CLI (`ps.py`) and its deterministic query surface (`query1`'s
  template router, `query2`'s catalog) already exist and are proven —
  evidence grounding and canonical-path answering both reuse them directly.

### What we can reliably answer, by question type

Everything above classifies *mistakes*. This classifies *questions* —
the statement this spike's own Purpose promised and the piece that was
missing until this was built. Tier was rejected as a classifier (above).
What replaces it isn't Stage 1–2's structural predicates alone either —
those catch specific triggers within a type, but don't say which types are
safe by default. Built by reading the actual question text (`dev-questions.md`,
`blind_questions.tsv` — not tier labels, not RUNBOOK's result summaries) and
cross-referencing against the pass/fail data already established, across
all 162 question-instances (54 questions × 2 CLI runs + 54 held-out
questions × 1 raw-Cypher run):

| Type | Example | Pass rate | Status |
|---|---|---|---|
| **A. Single-fact lookup** | "What's the status of X?" | 26/26 — 100% | Reliable, no known failure mode |
| **E. Cross-entity/regulation comparison** | "Do CRA and NIS2 apply to similar actors?" | 18/20 — 90% | Reliable |
| **H. Refusal-expected (gap check)** | "Worst fine we could face under GDPR?" | 7/7 once the Known-Gaps Registry exists | Solved (mechanism-confirmed, not just observed) |
| **B. Exact-set enumeration** | "What obligations does Manufacturer carry?" | 32/38 — 84% | Reliable *unless* a status filter (overdue/current/stale) is embedded in the criterion — Stage 1 alias table's job |
| **C. Aggregate/count** | "How many of our 68 capabilities are covered?" | 15/19 — 79% | Reliable *unless* hand-tallied — Stage 2's tool-computed-number requirement is the fix |
| **D. Chain/multi-hop trace** | "Trace CRA Art 13.1 to what it requires." | 9/12 — 75% | Reliable *unless* it's the "if X breaks, what's affected downstream" hypothetical variant — Stage 4 scope-match's job |
| **F. Status/definitional judgment** | "Are we compliant with GDPR Art 32?" | 9/16 — 56% overall, but **1/5 (20%) on the CLI path**, 5/6 (83%) on raw Cypher | Not yet reliable — looks fixable, see below |
| **G. Open recommendation/critique** | "Is that even the right way to think about criticality?" | **10/20 — 50%, on both tool surfaces** | Not yet reliable — looks like a genuine ceiling, see below |

**F and G fail for different reasons, and that difference is the actionable
part.** F's failure rate depends heavily on which tool answered it (20% CLI
vs. 83% raw Cypher) — a strong signal this is a summarization/
verdict-construction problem specific to the CLI's raw-row output, the same
shape as Miscount, and plausibly fixable the same way: a tool-computed
verdict field, analogous to `row_count`. G's failure rate is ~50% on
*both* tool surfaces — that's the signature of a genuine reasoning
ceiling, not a retrieval or tool-surface artifact, and matches the
question catalog's own tier definition, which admits Hard questions may
have "no single correct answer."

**This is a first-pass classification** — some questions blend types and a
primary type was assigned by judgment call where they did. Treat exact
percentages as indicative, not final; the F-vs-G distinction and the
overall ranking are robust to that uncertainty.

This table is derived data, not a fixed judgment — it should be
recomputed as Stage 5's audit loop accumulates more graded instances per
type, the same versioned-growing-artifact treatment as the failure-kind
taxonomy and the alias table.

## The Design

The pipeline is two phases, not one, because not every failure kind is
visible before an answer exists. **Phase A (Stages 1–2) predicts risk from
the question text alone** and drives routing. **Phase B (Stage 4) checks a
drafted or composed answer against evidence** and catches kinds Phase A
cannot see by construction. See the failure-kind → mechanism table above —
three of six kinds are Phase-B-only.

### Stage 1 — Term-coverage check (deterministic, Phase A)

Extract the domain terms a question turns on, **and record which canonical
entity-type it asks about** (e.g. "chain" vs. "control" — used by Stage 4's
granularity check, not resolved here). Match vocabulary terms against a
**per-term alias table** derived from `ps-domain-concepts.md` and the
skill's Canonical Definitions section — not a generic thesaurus; aliases
are curated per canonical term and reviewed, because domain status terms
that sound synonymous (stale/overdue/deprecated/redundant) are often
deliberately distinct (see the AU-H2 example above).

Three outcomes per term:
- **Exact match** — term is defined, proceed.
- **Alias/near-match** — term is defined but phrased differently. Surface
  the matched canonical definition explicitly to the composition step, and
  require the fitness gate (Stage 4) to confirm that specific definition —
  not a looser reading — was actually applied.
- **No match** — term is undefined. Do not attempt full synthesis.

**What this stage does and doesn't cover, precisely:** it catches undefined
*vocabulary* (AU-M4's case — "stale" applied with broader scope than its
definition). It does **not** catch an undefined *interaction* between two
individually-defined terms (SEC-M2/SEC-M4 — "overdue" and "deprecated" are
each defined; whether an overdue-bucket question implicitly excludes
deprecated entries is an undefined composition rule between them, not a
vocabulary gap). That's Stage 4's rule checks. It also doesn't catch
over-claiming or most granularity-slips, which are properties of the
drafted answer, not the question — also Stage 4.

The alias table starts from the skill's existing Canonical Definitions and
`ps-domain-concepts.md`, and grows the same way the Known-Gaps Registry
does: every new vocabulary-gap failure a future run finds becomes a new
table entry. It will never have full coverage of genuinely novel phrasing —
that's expected, not a flaw; a "no match" result on a truly new term is the
correct, intended output.

### Stage 2 — Structural risk classification (deterministic, Phase A)

- **Count-shaped** questions ("how many," aggregate language) → require a
  tool-computed number (`row_count` or equivalent) at the fitness gate,
  never a hand-tally.
- **Multi-part/exhaustive-enumeration-shaped** questions ("all applicable,"
  conjunctive requirements) → require decomposition into sub-claims, each
  checked for presence before composing.

### Stage 3 — Routing

Routing consults two independent signals, not one: Stage 1–2's per-question
mechanical checks (term-coverage, count-shaped, multi-part), and the
question's semantic **type** (A–H, above) with its measured base
reliability. The two aren't redundant — type-C's fix (tool-computed count)
and type-B's fix (alias table) are exactly the Stage 1–2 triggers, but
types F and G have no known trigger-based fix at all; their low reliability
is a property of the type itself, not a specific detectable flaw in a given
instance of it, and routing has to treat that differently.

- Full term coverage + known canonical path, **and a type with near-100%
  measured reliability (A, E) or a solved gap-check (H)** → answer directly
  via the canonical path, fitness-gated, no disclaimer beyond normal
  provenance citation.
- Full term coverage + known canonical path, **and a type with a known
  specific trigger (B, C, D)** → answer directly, but the relevant Stage
  1–2/4 trigger check is mandatory, not optional (alias table for B,
  tool-computed count for C, independent re-derivation + scope-match for
  D's hypothetical-chain variant).
- Needs decomposition (multi-part, an alias/near-match term, no
  single-shot canonical path, **or type F/G**) → decompose into
  sub-questions, route each recursively.
  - All sub-questions resolve via reliable paths → answer each
    individually (each fitness-gated), then **compose** — and the composed
    answer must *also* clear the fitness gate. Most observed completeness
    failures (SA-H2, PM-H1, PM-H2, RM-H2) had fully correct sub-facts that
    were dropped during composition, not during retrieval — decomposition
    fixes "did we get the right pieces," not "did we assemble them right."
  - Decomposition can't be reliably achieved → refuse, naming the specific
    reason (which term, or why it doesn't decompose).
- **Type G specifically** → bias toward a hedged block (B) — "draft, not
  verified" — over a confident synthesized claim, as the *default*, not
  the fallback: the measured ~50% reliability holds on both tool surfaces,
  so there's no known fix to attempt first the way there is for every
  other type. Block (C) is unaffected — it's always populated regardless
  of what (B) says. See the three-block output contract below.
- Any undefined term (Stage 1 "no match") → never full synthesis regardless
  of structural shape. Refuses, naming the term, or returns a hedged (B)
  with (C) still fully populated — never a synthesized claim delivered as
  more certain than (A) states.

Note: routing a question to the direct-answer path is not a reliability
guarantee by itself — Stage 4 still applies to it, and is where the three
Phase-B-only failure kinds (query-construction, over-claiming, undefined
interaction rules) get caught regardless of how clean Stage 1–2 looked.

### Stage 4 — Fitness gate (applied uniformly, canonical or composed path;
Phase B, post-answer)

- **Rule checks** — deterministic, formalized `SKILL.md` rules and the
  Known-Gaps Registry. Also the owner of undefined-*interaction* failures
  Stage 1 cannot see (SEC-M2/SEC-M4's overdue/deprecated composition-rule
  case), and of output-format compliance (real IDs in canonical form,
  required structure present) — Barnett et al.'s "Wrong Format," a named
  literature category with zero confirmed instances in our 33 so far.
- **Evidence grounding — existence.** Deterministic; re-queries the graph
  independently of whatever path produced the original answer. Must not
  reuse the original query — that only re-confirms a possible mistake
  instead of catching it (the lesson of SWE-M1/RM-E1's query-construction
  misses, 5/33 of the retroactive tally).
- **Evidence grounding — scope match.** A stricter, separate check from
  existence: does every regulation/entity/unit *named in the claim* appear
  in the specific evidence retrieved for it, at the same specificity — not
  just "does some related fact exist somewhere." Existence-checking alone
  passes a claim like AU-H4's "this capability weakens GDPR and NIS2 duties
  too" as long as the capability itself is real; scope match is what
  catches the claim over-extending past what was actually retrieved. Also
  cross-checks the answer's stated entity-type against the one Stage 1
  recorded from the question (catches EM-E3-style granularity mismatches —
  answering in "controls" when the question asked about "chains").
- **Semantic similarity** — statistical drift detection, not model-judged.
- **LLM judge** — reserved for the narrow residual no deterministic signal
  resolves. Run as an ensemble (n≥3), inter-run agreement recorded, not
  assumed.
- **Human review** — escalation when the judge and deterministic signals
  disagree, or a claim resolves by none of them.

### Stage 5 — Continuous audit and mechanism growth (lagged, out-of-band)

Stages 1–4 are a closed set of specific catches, each built for a
previously-observed failure shape — necessarily incomplete. A genuinely new
kind that trips nothing above passes Stage 4 clean and is delivered as
verified. Decision: **lagged**, not live. A universal (non-residual) judge
pass would catch new kinds in real time but reintroduces the per-answer
model non-determinism Stages 1–4 exist to route around. The accepted
tradeoff: a new failure kind ships un-caught until the next audit cycle, in
exchange for keeping the per-answer path deterministic-first — paired with
an explicit loop so the gap shrinks over time rather than sitting static.
This formalizes, as a recurring process, what this spike's own scoping did
once by hand against already-graded transcripts:

1. **Sample** — periodically pull a batch of pipeline-verified
   (non-escalated) answers. Risk-weighted, not uniform: prioritize Stage 1
   alias/near-match flags, comparison/relation-shaped claims, and
   decomposed-and-composed answers (16 of 33 known failures were
   completeness failures at composition — the single largest concentration
   found this session). A smaller random baseline sample covers shapes not
   yet hypothesized at all.
2. **Audit** — human review (not a model call — the goal is catching what
   models miss) checks each sampled answer against independently
   re-derived evidence and rules.
3. **Classify** — anything wrong found: an existing taxonomy kind an
   existing mechanism should have caught but didn't (fix/extend it), or a
   genuinely new kind?
4. **Promote** — a new kind is classified the same way every Stage 4
   mechanism already was: reduces to a structured, recomputable graph
   check → new deterministic Stage 4 sub-check; irreducibly a judgment
   call → named explicitly in the judge's brief and the human-review
   trigger criteria, so future instances of that shape get escalated even
   without a deterministic check.
5. **Regression-check** — any new/modified mechanism is validated against
   the full set of previously-confirmed cases before deployment — same
   must-flag/must-not-false-flag discipline already used for the alias
   table and scope-match.
6. **Version** — the failure-kind taxonomy and its mechanism set are a
   versioned, growing artifact, not a fixed list; each promotion is logged
   with what was found and why.

### Output posture — the three-block contract

**The system never returns just an answer.** Every response is exactly
three blocks, always, with no separate "full answer" vs. "evidence-only"
vs. "refuse" output modes — those collapse into variations of what blocks
A and B contain, not different formats:

- **(A) Confidence statement.** Always present, always explicit — never
  silence-implies-confidence. Derived directly from the type-reliability
  table and Stage 4's gate result, not a generic hedge:
  - Types A, E, H, and B/C/D once their specific trigger check has cleared
    → *"Given the data currently in the system, this is correct."*
  - **Type F** → *"Best-effort answer. Questions of this kind matched
    expert grading in ~56% of validation cases; on this system's current
    answering path specifically, ~20% — this looks like a fixable
    summarization gap, not a fundamental limit. Verify against (C) before
    relying on this."*
  - **Type G** → *"Best-effort answer. Questions of this kind matched
    expert grading in ~50% of validation cases, consistently across two
    independently-built answering paths — this is a draft requiring human
    judgment, not a verified conclusion. Verify against (C)."*
  - Refused / undetermined → states the specific reason (undefined term,
    decomposition failure, confirmed Known-Gaps Registry entry), not a
    generic "I don't know."
- **(B) Answer.** The claim itself — a real answer, a hedged draft, or an
  explicit "not determinable from what's in the system," depending on what
  (A) established. Never presented as more certain than (A) states.
- **(C) Verification data.** Not a citation alone — a citation makes the
  user go re-read the regulation to catch a summarization or comparison
  error, which defeats the purpose of a check fast enough that someone
  will actually do it. (C) carries **both** `source_ref` provenance chains
  (verifies against the original regulation text — catches extraction
  errors) **and** the concrete structured values the answer's claims are
  built from — the actual numbers, dates, IDs, rows (verifies the
  system's own arithmetic/comparison/summarization — catches errors like a
  relational inversion, where every individual fact cited is real and the
  *relationship* stated between them is backwards). These aren't two
  builds — (C) is Stage 4's independently re-derived evidence, already
  computed for the internal gate, rendered for the user instead of staying
  internal-only.

**Why (A) and (C) work together, not just alongside each other:** (A)'s
confidence level calibrates how much scrutiny (C) is worth. That
calibration is deliberately inverted from where it would default: loudest
on F and G, where the pipeline's own reliability is weakest and a human
check matters most; quiet on A/E/H, where a blanket hedge would just be
noise nobody reads. This is a second, complementary answer to the "what
happens when a new failure mode appears" question (see Stage 5 above) —
Stage 5's audit is lagged and sampled; a user checking (C) on their own
answer is live and applies to any failure kind, known or not, at the cost
of only working when the user actually looks. It doesn't replace Stage 5
(low-effort users on high-confidence types won't check, which is exactly
where Stage 5's sampling still has to do the work) — it changes the risk
profile, especially on the two types where it's needed most.

**Build implication:** Stage 4's evidence-grounding sub-stages can no
longer treat their output as an internal pass/fail boolean only — the
underlying re-derived data has to be structured for legible presentation,
not just a gate decision. This is part of the pipeline's output contract,
not an optional nice-to-have.

The confidence-statement text in (A) is generated from the type-reliability
table, not hardcoded — as Stage 5's audit loop updates the table with more
graded instances per type, (A)'s stated figures update with it.

## The Test

### Setup

1. Build the per-term alias table from `ps-domain-concepts.md` and
   `ps-domain/SKILL.md`'s Canonical Definitions section, including
   per-question entity-type extraction (Stage 1).
2. Build Stage 2's structural classifier (count-shaped, multi-part).
3. Build Stage 4's four deterministic sub-stages, reusing `ps.py`: rule
   checks, existence grounding, scope-match grounding, entity-type
   cross-check.
4. **Validate each mechanism retroactively against the specific case it
   claims to catch, before building Stage 3's routing or the judge/human
   paths** — a mechanism that hasn't been checked against its own target
   case isn't trustworthy to route on:
   - Stage 1 alias table: flags AU-M4 (undefined vocabulary), does **not**
     flag AU-H2 (adjacent-but-distinct terms correctly kept separate — the
     non-regression check) or SEC-M2/SEC-M4 (these are interaction-rule
     failures, out of Stage 1's scope by design — a Stage-1 flag here would
     itself be a bug, not a win).
   - Stage 4 rule checks: flags SEC-M2 and SEC-M4 (the interaction-rule
     cases Stage 1 correctly does not touch).
   - Stage 4 scope-match: flags AU-H4 and SEC-H4 (over-claiming), passes
     the true-but-narrower claims in the same transcripts that didn't
     over-extend.
   - Stage 4 entity-type cross-check: flags EM-E3 and EM-M4
     (granularity-slip).
   - Stage 4 existence grounding (independent re-query): resolves the 5
     dev-v2b cases manual grading left ambiguous (SA-H1, SA-H2, SEC-E1,
     SEC-H1, CO-H2) with a definitive verdict.
5. Only after step 4 passes on every mechanism individually, build Stage 3
   (decomposition/composition routing) and the narrowed judge/human paths,
   and re-run end to end against all 162 already-graded question-instances.
6. **Dry-run Stage 5's sampling strategy retroactively**, using the 162
   already-graded question-instances as a stand-in for "pipeline-verified
   answers" (a proxy, not a live test — see "What This Is NOT"): apply the
   risk-weighted sampling rule (step 1 of Stage 5) to the pool of
   transcripts Stage 1–4 marked verified, at a fixed sample size, and check
   how many of the known 33 failures a sample of that size surfaces.
   Compare against a uniform-random sample of the same size as the
   baseline. This doesn't require live production volume to test whether
   "risk-weighted" is actually doing better than "random" — the known
   failures and their transcripts already exist.

### Success Criteria

| Criterion | Threshold |
|---|---|
| **Vocabulary-gap precision** | Stage 1 flags AU-M4, does not false-flag AU-H2, and does not (incorrectly) flag SEC-M2/SEC-M4 |
| **Interaction-rule coverage** | Stage 4 rule checks flag SEC-M2 and SEC-M4, which Stage 1 correctly misses |
| **Scope-match precision** | Stage 4 flags AU-H4 and SEC-H4 without false-flagging correctly-scoped claims elsewhere in the same transcripts |
| **Granularity precision** | Stage 4's entity-type cross-check flags EM-E3 and EM-M4 |
| **Ambiguity resolution** | The 5 dev-v2b cases manual grading couldn't resolve get a definitive existence-grounding verdict via independent re-query |
| **Miscount elimination** | Every count-shaped question produces a tool-computed number at the fitness gate; 0 hand-tallied numbers reach a final answer |
| **Judge ensemble reliability** | Inter-run agreement rate for the narrow residual LLM judge stage is measured and reported, not assumed |
| **No false auto-pass** | Zero tolerance: the pipeline must not mark as verified any answer RUNBOOK.md graded as a failure |
| **Auditability** | Every verdict is traceable to a specific stage and mechanism — not a black-box aggregate |
| **Sampling efficiency** | Stage 5's risk-weighted retroactive sample surfaces a higher fraction of the known 33 failures than a uniform-random sample of the same size — if it doesn't, the risk-weighting rule itself needs revision before it's trusted on live data |
| **Three-block completeness** | Every output in the retroactive run has non-empty (A), (B), and (C) — zero instances of a bare answer with no confidence statement or verification data |
| **Block (C) sufficiency for relational claims** | For every comparison/relation-shaped claim in the retroactive set, (C) contains both compared structured values, not just source_ref citations — sampled manually: would a reader with (C) in front of them have been able to catch the AU-H4/SEC-H4 over-claiming instances without re-deriving anything themselves? |

### Failure Modes to Watch

- Alias table over-matches (groups deliberately distinct terms as
  synonyms) — the AU-H2 trap.
- Alias table under-matches on genuinely novel phrasing — expected and
  acceptable; the fallback (refuse/evidence-only) is correct behavior, not
  a bug, as long as it doesn't silently degrade to a synthesized answer.
- Rule checks written too rigidly, false-flagging a legitimate interaction
  the golden answer just didn't need to state (rule checks generalize from
  a small number of confirmed cases — SEC-M2/SEC-M4 alone is a thin base).
- Scope-match check too strict, false-flagging a claim that legitimately
  synthesizes across a broader evidence set than any single query returned
  (multi-hop composition is supposed to combine facts — the check needs to
  distinguish "combined correctly" from "over-extended").
- Evidence-grounding re-uses the original (possibly flawed) query instead
  of an independent one — reproduces the SWE-M1/RM-E1 miss one level up.
- Decomposition finds the right sub-facts but the composition step still
  drops one — the exact failure mode decomposition alone does not fix; the
  composed-answer fitness gate is what has to catch it.
- LLM judge ensemble shows low agreement — would mean the "one place
  judgment is irreplaceable" isn't reliable enough to anchor even a narrow
  residual, a significant finding in its own right.

## What This Is NOT

- Not an autonomous compliance-decision engine — output is an
  evidence-annotated answer; a human makes the compliance call.
- Not re-litigating `cli-tool-semantics`'s command-routing findings —
  resolved per dev-v2b.
- Not building a production human-review UI — only the escalation signal
  and its structured reason.
- Not a held-out generalization check on this pipeline itself — this spike
  validates against the already-graded transcripts (dev and held-out
  questions from prior spikes, used here only as fixed input data, not as
  a live generalization test); a genuine held-out check on the pipeline is
  later work.
- Not a live run of Stage 5 — setup step 6's retroactive dry-run tests
  whether the *sampling rule* is smarter than random using known,
  already-labeled failures. It cannot test the harder live question (does
  the audit catch a failure kind nobody has hypothesized yet), because
  every failure in the 162-question-instance pool is by definition already known.
  A real Stage 5 cycle against live, previously-unseen pipeline output is
  later work.

## Deliverables

- The per-term alias table (versioned, reviewable, growing the same way
  the Known-Gaps Registry does).
- A pipeline prototype implementing Stages 1–4, reusing `ps.py` for
  canonical answering and evidence grounding.
- Stage 5's sampling rule and audit procedure, specified and dry-run
  (setup step 6) against the retroactive pool — not yet run live.
- A report applying the pipeline to the 162 already-graded question-instances
  (`cli-tool-semantics` × 2 runs + `skill-transfer` held-out), with
  explicit resolution of the 5 ambiguous dev-v2b cases and a pass/fail
  against every success criterion above.
- An updated verdict on AD-7.
