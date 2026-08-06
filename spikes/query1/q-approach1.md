# Approach 1: Deterministic Parameterized-Template Router

The cheapest mechanism worth trying first, per `README.md`'s own prediction
("this tier is where a thin NL→Cypher translator should be enough" /
"a small fixed set of parameterized query templates is sufficient") and
`example-questions.md`'s prototype notes. No LLM in the loop — this
environment has no `ANTHROPIC_API_KEY` configured, and more importantly, a
template router establishes a deterministic baseline to beat before paying
for an LLM-in-the-loop or RAG mechanism at all. If a fixed pattern-match +
Cypher-template approach already clears the Simple/Medium tier, that's the
honest floor a fancier mechanism needs to justify itself against.

## Design

1. **Entity resolver** — loads every `Role.name`, `Capability.name`,
   `Policy.title`, and `Regulation.id` prefix from the live graph once at
   startup, builds a case-insensitive lookup, and resolves free-text/quoted
   mentions in a question (`'Security Logging'`, `Manufacturer`, `GDPR`) to
   real ids. No fuzzy ML matching — exact and substring match only. This is
   deliberately unambitious: it should fail loudly (no match) rather than
   guess.

2. **Template library** — one entry per question *shape* (not per literal
   question string): a regex to recognize it, a parameter extractor, and a
   parameterized Cypher query. Ordered most-specific-first so a router match
   is unambiguous.

3. **Governance/trust annotation, not filtering** — this is the piece that
   comes directly out of the "two failure modes" discussion. For any
   template that walks into Capability→Policy→Standard→Control, the query
   itself computes an explicit `is_current_evidence` boolean per row —
   `policy.status = 'approved' AND standard.implementation_status IN
   ['implemented','reviewed'] AND control.implementation_status =
   'implemented'` — rather than silently filtering rows out, and rather
   than leaving that judgment to prose in a rubric. The mechanism always
   returns the full structural answer *plus* the trust flag; it doesn't
   decide compliance on the user's behalf, but it can never present a stale
   chain as indistinguishable from a current one. This targets the
   governance/ratification problem specifically — it does **not** attempt
   to catch graph-health issues (contradictory status combinations,
   dangling edges); that's a separate concern for a validation pass against
   the graph itself, out of scope here.

4. **Router** — tries templates in order; first match wins, extracts
   params, runs the query. No match → an explicit `NO_TEMPLATE_MATCH`
   result, never a guess. This is the mechanism's only way of being honest
   about the questions it isn't built for yet.

## Scope for this pass

**In scope** (exact-match / set-match questions from `golden-answers.md`,
templatable as structural Cypher):
S1, S2, S3, S4, S5, S6, S7, S8, M1, M2, M4, M6, M7 (with trust flag), M8,
H2, H4, H7.

**Explicitly out of scope** (rubric-graded, need semantic/NL reasoning a
fixed template can't do — this is the honest boundary of this approach, not
a bug):
M3, M5, H1, H3, H5, H6.

**Re-scoped after `example-questions.md`'s second pass (2026-08-06).**
S9–S15/M9–M14/H8–H15 added Software Engineer / Security Engineer /
Engineering Manager questions, deliberately including ones with no named
entity to anchor on. 7 of the 16 turned out to be just as templatable as
the original 17 — deterministic aggregates/filters don't actually require
an entity anchor, they just need the right Cypher shape (M9–M13's
overdue-control counts, coverage-gap set, draft-standard filter; S9/S10's
Policy→Control chain and fuzzy Control-title lookup). Newly in scope: **S9,
S10, M9, M10, M11, M12, M13** (7 more templates, `M9M12` sharing one
handler since they're the same underlying query in two response shapes —
see `golden-answers.md`'s note on why that sharing matters, not just
convenient). Newly out of scope, confirmed via honest `NO_TEMPLATE_MATCH`:
**M14, H8, H9, H10, H11, H12, H13, H14, H15** — 9 more, including two that
aren't a mechanism gap at all: H10 and H15 ask about data the graph
structurally doesn't have (a Service node, a status-transition history),
not data a smarter query mechanism could find. In-scope total: **24**.
Out-of-scope total: **15**.

## Known catalog wrinkle surfaced while building this

H2's question text says "capabilities **required by CRA** [that] have no
governing Policy" — read literally and scoped to only CRA-required
capabilities, the live answer is **22**. But the golden answer already
published in `synthetic-data-spec.md` and carried into `golden-answers.md`
is the **global** ungoverned count across all regulations (**55**), and was
explicitly confirmed live there. This implementation matches the
already-published global golden (55) for consistency with existing docs,
but the mismatch is real and unresolved: either the question text should
drop "by CRA," or the golden answer should be rescoped. Flagging here
rather than silently picking one.

## Test plan

Run every in-scope question through the router, compare the result to the
golden values in `golden-answers.md`, report pass/fail per question and a
tier summary. Success bar: 100% pass on in-scope questions, and a clean
`NO_TEMPLATE_MATCH` (not a wrong answer) on every out-of-scope one.

## Files

- `query_mechanism_v1.py` — entity resolver, template library, router.
- `test_query_mechanism_v1.py` — golden-value test harness and report.

## Result: 39/39 (`python3 test_query_mechanism_v1.py`)

Originally 23/23 (17 in-scope + 6 out-of-scope). Re-run against the
augmented catalog after `example-questions.md`'s second pass added 16
Software Engineer / Security Engineer / Engineering Manager questions:
**39/39** — 24 in-scope questions pass against golden (7 new templates
added, see "Re-scoped" above), 15 out-of-scope questions correctly refuse.
The ratio holding up (roughly 60% templatable before and after) is itself
informative: adding audiences who ask more **open** (unanchored) questions
didn't collapse the deterministic-template floor, because about half of
their questions turned out to be anchor-free but still deterministic
(M9–M13) rather than genuinely open-ended. Only the questions that need
NL-to-Capability mapping, backward multi-hop reasoning, or whole-graph
synthesis (M14, H8, H9, H11–H14) — plus the two genuine schema gaps (H10,
H15) — actually need something past a template.

Two things fell out of actually building and testing this, beyond just
"does the router work":

1. **A hand-eyeballed golden count was itself wrong.** S3's obligation set
   was recorded as 47 in `golden-answers.md` from manually scrolling a
   printed list; the router (and an independent re-check) both give 48.
   Fixed in `golden-answers.md`. Lesson: don't hand-count 40+ row lists,
   verify with `count()` — the same discipline this whole exercise is
   supposed to be enforcing on the query mechanism applies to producing the
   golden values in the first place.

2. **A real FalkorDB query-engine reliability issue**, found while wiring
   M7's trust-flag chain: the identical 6-hop `MATCH` pattern returned
   **33** or **49** rows instead of the correct **57**, depending on which
   columns were projected in `RETURN` — e.g. projecting only `req.id`
   (or `req.id` plus a few properties, but not all six matched node ids)
   silently dropped rows; projecting all six ids gave the right answer.
   Confirmed against an independent Python-side join of each hop pulled
   separately. This is a third category on top of the two discussed
   earlier (data health vs. governance filtering) — **query-engine
   correctness** — and it's arguably the most dangerous of the three,
   because it fails silently and would have shipped a wrong M7/H1 golden
   answer if the trust-flag work hadn't forced a projection change that
   exposed it. Mitigation applied here and worth carrying into any future
   mechanism: for chains of 5+ hops, always project all matched node ids
   (or `RETURN DISTINCT` over all of them), and spot-check long-chain
   counts against an independently-computed join rather than trusting one
   Cypher query's row count. See `golden-answers.md`'s M7 section for the
   full writeup.

## Next

This establishes the floor: 24 of 39 questions are fully solved by
deterministic templates plus a governance-aware trust flag, no LLM
required. The remaining 15 (M3, M5, M14, H1, H3, H5, H6, H8, H9, H11, H12,
H13, H14 — needing NL-to-Capability mapping, semantic similarity, backward
multi-hop reasoning, or whole-graph synthesis — plus H10 and H15, which are
schema gaps no query mechanism can close) are what `q-approach2.md` is
scoped against, precisely by what's left rather than by the whole catalog.
