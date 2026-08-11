<!-- © 2026 Cartman ApS. All rights reserved. -->
# Progress Tracker — E2E Pipeline Implementation

Resumable build state for [README.md](./README.md)'s design.

## Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Claim representation: harness emits structured claims alongside prose vs. parsing prose after the fact | **Resolved** — structured claims; schema below |
| D2 | Fork `pipeline/*` + `ps.py` into this spike; no live cross-spike import | **Resolved** — see [[spike-independence-no-shared-code]] |
| D3 | Harness wiring: extend `ps-domain/SKILL.md` directly vs. a separate addendum scoped to this spike | **Resolved** — spike-local addendum first, fold into `SKILL.md` once proven |
| D4 | Relationship to `end-to-end-slice`: keep separate | **Resolved** |

### D1 detail — claim schema

`compliance-decision-pipeline`'s `pipeline/fitness.py` has no unified claim
type — each of its 7 Stage 4 checks takes its own ad hoc argument shape,
today hand-derived per test case by a human reading answer prose
(`tests/run_target_cases.py`). D1 replaces that human with the harness: one
claim kind per check, same "thin, not speculative" discipline `types.py`
already documents for itself.

| Claim kind | Fields (harness asserts) | Feeds |
|---|---|---|
| `overdue_set` | `control_ids: [str]` | `check_overdue_excludes_deprecated` |
| `cited_ids` | `ids: [str]` | `check_existence` **and** `check_completeness` (same claim, both directions) |
| `regulation_scope` | `regulations: [str]` | `check_regulation_scope` |
| `counting_unit` | `entity_type: str` | `check_entity_type_match` |
| `fanout_maximum` | `capability_id: str, count: int` | `check_fanout_maximum` |
| `evidence_gap_category` | `category: governance\|engineering\|resolved\|excluded` | `check_evidence_gap_root_cause` |

**Claim vs. context.** Not every fitness.py parameter is a claim to verify.
`reference_date`, `independent_query`, and (usually) the anchor
`capability_id` are pipeline-supplied context for constructing the
independent re-query — letting the harness supply those would be letting it
grade its own homework. `fanout_maximum` is the exception: both fields are
genuinely claimed (the answer asserts *which* capability *and* its count).

**Capability-id gap surfaced by this decision.** Several checks need a
`capability_id` anchor that no Stage 1 mechanism extracts from live question
text (Stage 1 only gives `entity_type` and term matches). Resolution: the
claim payload always carries the `capability_id`(s) the answer's evidence
was drawn from — the harness already has this from whatever `ps query
catalog` / `ps capabilities list` call it made — rather than having the
adapter re-derive it from question text.

**Emission mechanism (tool arg vs. CLI flag vs. fenced block) is D3's
concern, not D1's** — this schema is the payload shape, independent of how
it reaches the CLI.

### D3 detail

`ps-domain/SKILL.md` is the canonical shipped skill (AD-6) — the only copy
the harness reads in a live turn. Editing it now would wire instructions for
a CLI command that doesn't exist yet (no entrypoint, no adapter) into the
doc every other ps-domain consumer reads today. Same caution D2 already
applied to code, applied here to the skill: build CLI entrypoint + adapter
in this spike, ground the harness via a spike-local addendum, validate
against real live questions, then fold the proven instructions into
`SKILL.md` as this spike's final deliverable — not before.

**Consequence:** the addendum alone can't satisfy the "live round trip"
success criterion until it's folded in, so that fold is a required last
step of this spike, not optional cleanup.

## Environment

- FalkorDB at `localhost:6379`, graph `policy_system`, via `/usr/bin/python3` (not repo `.venv`)
- Query surface + verification logic: this spike's own forked copies (D2)
- `ps-domain` skill and FalkorDB itself stay shared — not spike-local
- **2026-08-10 finding, resolved same session:** the `falkordb` Python
  client was missing under `/usr/bin/python3` in this shell (server was
  reachable, client wasn't). Fixed via
  `/usr/bin/python3 -m pip install --user falkordb` (installs to
  `~/Library/Python/3.9/lib/python/site-packages`, no venv/sudo — that
  interpreter's own site-packages is root-owned). Confirmed live end to
  end afterward: `ps.py capabilities list`/`query catalog` and
  `pipeline_cli.py query` both ran against the real graph — a correct
  8-obligation `cited_ids` claim composed a confident answer with real
  `source_refs` (regulation/article text resolved), and a deliberately
  incomplete claim (2 of the 8 real ids) was correctly caught and flagged
  by `completeness_grounding`, not silently passed.

## First live-question batch

Two previously-unasked questions, anchored on `cap_availability_resilience_7caf2b`
("Availability & Resilience," 4 real obligations spanning CRA-1.0/GDPR-1.0/
ENGPRAC-3.0) — deliberately not `compliance-decision-pipeline`'s existing
target-case capability (checked first: `cap_access_control_authentication_151816`,
the obvious first pick, is already SEC-H1's anchor).

| Question | Type | Claim | Outcome |
|---|---|---|---|
| "Which obligations require our Availability & Resilience capability, and which regulations do they come from?" | B | `cited_ids`, all 4 real obligation ids | (A) confident, (B) verbatim answer, (C) `existence_grounding` + `completeness_grounding` both pass, real `source_refs` resolved for all 4 |
| "If our Availability & Resilience control failed its next review, which regulations would be affected?" | D | `regulation_scope`, [CRA-1.0, GDPR-1.0, ENGPRAC-3.0, NIS2-1.0] — NIS2-1.0 deliberately over-claimed | Correctly routed DIRECT_MANDATORY_CHECK (hypothetical-chain regex fired on "failed its next review"); `scope_match_regulation_routing` flagged the NIS2-1.0 over-claim; (B) rendered `[FLAGGED -- not verified]`, not silently passed |

**Failure-kind comparison:** no new failure kind found — both results matched
what the pipeline's existing mechanisms predict. Not yet a broad enough
sample to close the "First live-question batch" success criterion generally,
just this session's batch.

## Second live-question batch — docs/test-data/dev-questions.md sample

6 questions from the dev catalog (54 total; 6 already `compliance-decision-
pipeline` target cases — AU-H2, CO-H2, SA-H1, SA-H2, SEC-E1, SEC-H1 —
excluded from the pick), spanning types A/B/C/D and different audiences.
Each answered fresh against the live graph (not copied from
`dev-answers.md`, which would trivially pass).

| ID | Question | Type | Claim(s) | Outcome |
|---|---|---|---|---|
| LC-E1 | Text of CRA Art. 13.1 | A | none — no check for regulation-text lookup | (A) confident, 0 checks — expected for type A |
| SA-E2 | Capability behind CRA's unauthorised-access duty | A | none — no check for obligation→capability lookup | (A) confident, 0 checks — expected for type A |
| EM-M1 | Count of overdue Controls (answer: zero) | C | `overdue_set` (empty), then `overdue_set` + `counting_unit` | First attempt (only `overdue_set`) flagged: type C's routing mandates `entity_type_cross_check` whenever Stage 1 records an entity type, which needs a `counting_unit` claim I hadn't supplied. Correct once added — my claim-extraction miss, not a pipeline bug. |
| PM-M1 | Governed capabilities w/ zero implemented Controls, and why | B | 2× `evidence_gap_category` ("resolved") | Both checks individually passed (claimed category matched independent derivation) but the overall gate still failed — see Finding 1 |
| RM-M1 | Capabilities required by >1 regulatory duty (52 by Obligation-count, 22 by distinct-regulation-count — both counted live, ambiguity flagged) | B | none — no matching check | Flagged not-verified — see Finding 1 |
| AU-M1 | Full chain trace, CRA Art. 13.1 → Control (non-hypothetical phrasing) | D | none — no matching check | Flagged not-verified — see Finding 2 |

**Finding 1 (new): no Stage 4 check grounds a capability-set enumeration
under a structural predicate.** `cited_ids` only verifies an obligation-id
list anchored at *one* capability (`adapter.py`'s independent query is
hardcoded to that shape). A question asking *which capabilities* satisfy
some predicate ("zero implemented Controls," "required by >1 Obligation")
has no corresponding independent re-derivation check — confirmed twice,
independently, on PM-M1 and RM-M1. Both answers were factually correct
(PM-M1's per-capability root-cause claims each passed on their own); the
gate failure is honest, not a false-flag on wrong data, but it means this
whole question shape currently can't clear a type-B route's mandatory-check
bar no matter how correct the answer is.

**Finding 2 (new, same shape as the known AU-M4 gap): no Stage 4 check
grounds a single, non-exhaustive chain-trace claim.** AU-M1 asks to trace
one specific path — claiming that path exists and is correct, not that it's
the capability's *complete* obligation set. `cited_ids` is the only claim
kind that could plausibly fit, but its independent query would check
completeness against *every* obligation requiring the capability, which
AU-M1's answer never claimed to be exhaustive about — using it would create
a false-omission flag on a correct, narrowly-scoped answer. Routing's own
docstring named this AU-M4 gap already (`stale_chain_strict_reading`); AU-M1
shows it isn't AU-M4-specific — it's the general shape of "trace one chain,"
not a per-check gap.

**Finding 3 (addendum wording gap, not a pipeline bug):** EM-M1 needed both
`overdue_set` and `counting_unit` claims together — `SKILL_ADDENDUM.md`
doesn't currently say a type-C count question needs a `counting_unit` claim
*in addition to* whatever domain-specific claim it also triggers, whenever
Stage 1 records an entity type. Worth a follow-up edit to the addendum.

**Failure-kind comparison for this batch:** 2 clean type-A passes, 1
self-corrected type-C pass, and 3 honest not-verified flags that are all
instances of the same two new structural gaps above (not 3 unrelated
misses) — no false auto-pass on any of the 6, but a real coverage gap on
question shapes real users (RM-M1, PM-M1, AU-M1 are all overview/audit-style
asks) are likely to want answered.

## Build status

| Component | Status |
|---|---|
| README.md | ✅ |
| PROGRESS.md | ✅ |
| D1 | ✅ resolved — schema above |
| D3 | ✅ resolved — addendum-then-fold, detail above |
| Fork `pipeline/*` + `ps.py` | ✅ — plus `query_mechanism_v1.py` + `catalog.py`, ps.py's real dependency closure (see below) |
| `Claim`/`ClaimSet` types (`pipeline/types.py`) | ✅ |
| CLI entrypoint (`pipeline_cli.py`) | ✅ — `pipeline query "<question>" --type A-H --answer <prose> --claims <json>` |
| Claim-schema adapter (`pipeline/adapter.py`) | ✅ — dispatches `ClaimSet` → fitness.py per claim kind; CITED_IDS scoped to capability-anchored obligation ids only (v0 gap, see adapter.py docstring) |
| Harness-side wiring (spike-local addendum) | ✅ — `SKILL_ADDENDUM.md`, scoped to every PS compliance question, D3's fold-in still pending |
| First live-question batch | ✅ — 8 questions across 2 batches, detail above; more batches needed before the criterion is broadly closed |
| Failure-kind comparison | ✅ for both batches — batch 2 found 2 new structural gaps (Findings 1-2 above), logged not absorbed |
| AD-7 verdict | ⬜ |

Fork note: `ps.py` imports `query_mechanism_v1` and `catalog` directly (not
just `pipeline/*`), so those two files were forked alongside it, and every
`sys.path` hack toward sibling `query1`/`query2` directories was removed
(including a dead one in `catalog.py`, confirmed unused before deleting) —
otherwise the "fork" would still import live across the spike boundary at
runtime. Verified with a stubbed `falkordb` module: full import graph
resolves with zero `spikes/query1`/`spikes/query2` entries on `sys.path`.

## How to run

```
/usr/bin/python3 pipeline_cli.py query "<question, verbatim>" \
  --type <A-H> \
  --answer "<proposed answer prose>" \
  --claims '{"claims": [{"kind": "overdue_set", "control_ids": ["ctrl_..."]}]}' \
  [--reference-date YYYY-MM-DD] [--question-id ID] [--format text|json]
```

`--type` is supplied by the caller (question-type assignment has no
classifier — `pipeline/question_types.py`'s own docstring). `--claims`
(or `--claims-file <path>`) is optional; omitted claims mean zero Stage 4
checks run, same vacuous-pass-then-gate-on-path behavior
`compose.compose_output` already had. Verified live against the real graph
(Environment section above) — not a mock.

## Next action

Two new structural gaps are now confirmed (Findings 1-2, second batch):
predicate-filtered capability-set enumeration, and single non-exhaustive
chain-trace claims. Before the next live batch, decide whether to (a) build
Stage 4 mechanisms for either shape, (b) accept them as named v0 gaps like
`stale_chain_strict_reading` and move on, or (c) narrow the addendum's
"every PS compliance question" trigger scope so questions of these shapes
aren't promised a verified result they can't get yet. Then continue toward
an AD-7 verdict (README Setup step 7, Deliverables) with a broader batch.
