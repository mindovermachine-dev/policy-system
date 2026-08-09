<!-- © 2026 Cartman ApS. All rights reserved. -->
# Progress Tracker — Compliance Decision Pipeline Implementation

**Purpose of this doc:** resumable state for implementing the pipeline
designed in [README.md](./README.md). Update this after every meaningful
step (code written, test run, decision made) — treat it as the thing a
fresh session reads first, before any code.

**Do not re-derive what's already settled here.** README.md is the design;
this doc is *build status against that design* plus the specific
implementation decisions made while turning prose into code (the design
doc is sometimes ambiguous about mechanics — resolutions are recorded below
so they aren't re-litigated).

---

## Chosen build strategy (agreed 2026-08-09)

Not a pure walking-skeleton-first approach, and not README's Setup section
followed to the letter either — a middle path:

- Build Stages 1, 2, and 4's four sub-checks as **real logic, thin
  coverage** — not full vocabulary/rule coverage, but no stubs that
  always-pass. A stubbed gate would be worse than no gate: it would make
  the skeleton look integrated while proving nothing about reliability,
  which is the exact failure mode this whole spike exists to avoid.
- Stage 3 (decomposition/composition routing), the LLM judge ensemble, and
  human escalation are legitimately stubbed/deferred in v0 — least urgent,
  matches README's own sequencing.
- **Test oracle is the 108 already-graded transcripts, not new questions.**
  Iterate against the specific known target cases below (ground truth
  already established) before ever running a new/unseen question through
  the pipeline. New-question testing is phase 2, once mechanisms pass their
  own target cases — that's what tests generalization, not correctness.
- One open gap, not yet addressed: none of the 108 transcripts were graded
  against an actual pharma-auditor acceptance bar (they used this project's
  own expert-grading rubric). Plan to add an explicit manual checkpoint
  against that bar once the skeleton passes its mechanical validation —
  see "Open questions" below.

---

## Environment (verified 2026-08-09)

- FalkorDB reachable: `/usr/bin/python3` (not the repo `.venv` — lacks
  `falkordb` package, same constraint `ps.py` documents) can connect and
  query `policy_system` graph on `localhost:6379`. 728 nodes confirmed live.
- Reuse [`spikes/cli-tool-semantics/ps.py`](../cli-tool-semantics/ps.py) as
  the query surface for Stage 4's graph-grounded checks — do not
  reimplement query logic, per README's Setup step 3.
- Canonical definitions and rules: `.github/skills/ps-domain/SKILL.md`.
  Key ones this pipeline encodes:
  - **Overdue** = Control's `next_review_date` passed; **deprecated
    Controls excluded** (left the review cycle, didn't fail it).
  - **Stale** = chain from Capability to a *current* Control is broken (no
    `IMPLEMENTED_BY` Control in `implemented`/`reviewed` status) — not
    merely an overdue review on an otherwise-live chain.
  - Rule 7 (narrowing): blast-radius claims must cite only chains that
    *route through* the named node, not siblings reaching the same
    downstream target another way.
- Question/answer catalogs:
  - `docs/test-data/dev-questions.md` + `docs/test-data/dev-answers.md` —
    dev-set questions/goldens (used by both `cli-tool-semantics` dev-v1/v2b
    and `skill-transfer` dev-v1).
  - `spikes/skill-transfer/blind_questions.tsv` — held-out question text
    (goldens for this set are **not** in this repo per the held-out
    discipline; RUNBOOK.md notes columns are the only source of grading
    detail available here — treat as sufficient for mechanism validation,
    not as a substitute for the real golden).

---

## Target cases (Setup step 4's validation set) — question text + what each mechanism must do

Pulled directly from `dev-questions.md`, `blind_questions.tsv`, and both
RUNBOOKs' per-question notes. This is the ground truth to build and test
against — do not invent new test questions until these pass.

### Stage 1 — alias table (must flag AU-M4, must NOT flag AU-H2 or SEC-M2/SEC-M4)

| ID | Question | Set | Why it's the target case |
|---|---|---|---|
| AU-M4 | "Which GDPR articles currently have only stale requirement-to-control evidence chains, and why?" | blind | Uses "stale" verbatim. Agent answer {33,37,38} vs golden {32.4,37,38} — over-broadened "stale" to include overdue-but-live chains. |
| AU-H2 | "Trace the CRA's actively-exploited-vulnerability reporting duty from the regulation text all the way into our internal governance — does the trail reach a check that's actually running?" | dev | Contains **neither** "stale" nor "overdue" nor "deprecated" verbatim — non-regression: Stage 1 must not spuriously trigger status-term disambiguation on a question that never uses that vocabulary. |
| SEC-M2 | "Which checks are overdue for review right now — not just due soon?" | blind | Uses "overdue" verbatim — **should** exact-match, but must NOT additionally flag an "interaction" concern (see resolution below). |
| SEC-M4 | "Which checks come due for review before the end of August 2026, and which are already overdue?" | blind | Same as SEC-M2. |

**Design resolution (this session):** Stage 1 operates on question text
only, before any retrieval. "Flagging AU-M4" does **not** mean refusing or
routing away — "stale" is a defined canonical term, so it's an EXACT match.
The flag is: attach the full canonical definition text as mandatory
routing metadata (`disambiguation_required: true` for a small curated
cluster of confusable status terms: stale/overdue/deprecated), which Stage
4's fitness gate must later confirm was actually applied (the strict
"broken chain" reading, not "includes lapsed review"). AU-H2 doesn't use
any clustered term, so no metadata attaches — that's the non-regression
check, not a different code path. SEC-M2/SEC-M4 *do* trigger the same
"overdue" exact-match + definition attachment — that's correct, expected
behavior, not a bug. What Stage 1 must NOT do is invent a second,
deprecated-interaction flag — that check is entirely Stage 4's (it can only
be evaluated against retrieved rows, which don't exist yet at Stage 1).

### Stage 2 — structural classifier (count-shaped, multi-part)

Not yet mapped to specific target cases with the same rigor — README's
Setup step 4 doesn't name specific Stage-2-only validation cases (Stage 2's
targets, per the failure-kind table, are Miscount and Completeness, both
of which are actually *caught* downstream at Stage 4/composition, not by
Stage 2 alone). Build Stage 2 as a pure question-text classifier
(count-shaped regex/keyword match, multi-part/conjunctive detector) and
spot-check against known Miscount cases for sanity: AU-M2, SEC-M3, EM-H2
(cli-tool-semantics dev-v1, all "how many" shaped) and known Completeness
cases: SA-H2, PM-H1, PM-H2, RM-H2 (multi-part/rubric questions).

### Stage 4 — rule checks (must flag SEC-M2 and SEC-M4)

Both fail the same way: **included a deprecated-status Control in the
"overdue" set** (with a caveat noting it was deprecated/moot, but still
included — golden requires exclusion, not exclusion-with-caveat). Rule
check: query `next_review_date < reference_date AND status <> 'deprecated'`
via `ps cypher`, treat that set as canonical, flag any answer whose stated
"overdue" set includes a deprecated-status Control at all.

Reference date anchor for all date-relative questions: **2026-08-01** (per
both RUNBOOKs).

### Stage 4 — scope-match (must flag AU-H4 and SEC-H4)

| ID | Question | Failure |
|---|---|---|
| AU-H4 | "If our log-retention check turns out to have failed, which regulatory requirements does that undermine?" | Golden: CRA-only (+ Helvex SOP) undermined; NIS2/GDPR explicitly **not** via this capability. Agent over-claimed GDPR/NIS2 "weakened" too, via the shared standard rather than the direct routed chain. |
| SEC-H4 | "If the Encryption-at-Rest check fails its review on August 15, which regulatory duties does that put at risk?" | Golden requires isolating Art. 32(1)(a) as the primary casualty, CRA side hedged. Agent listed duties verified by *other* controls (v2/v3) not actually failing — over-broad blast radius. |

Both are SKILL.md rule 7 (narrowing) violations: claim named a
regulation/entity reached via a sibling chain through the same
standard/policy node, not the chain that actually routes through the
specific named Control/Capability. Scope-match check: independently
re-derive the chain that *routes through* the named node only (not
`MATCH` on the shared Standard/Policy and fan back out), then flag any
regulation/entity named in the claim that isn't on that specific routed
path.

### Stage 4 — entity-type cross-check (must flag EM-E3, spot-check EM-M4)

| ID | Question | Failure |
|---|---|---|
| EM-E3 | "How many of our GDPR evidence chains would currently hold up in an audit?" | Question's entity type: **chains**. Agent answered "3 of 57," counting **controls** — golden is 31 of 57 chains. Right numbers, wrong unit. |
| EM-M4 | "How much of our GDPR evidence problem is a governance problem versus an engineering problem?" | Direction right, but the 16/10 split framed at a different sub-grouping than golden (Clinical-draft 10 vs incident-v2 10) — weaker/less clean a target than EM-E3 for this specific mechanism; treat as a stretch case, not a must-pass gate for v0. |

Stage 1 must record the question's stated entity type ("chain" vs
"control" vs "obligation" etc.) at capture time; Stage 4 cross-checks the
answer's stated counting unit against it.

### Stage 4 — existence grounding / independent re-query (resolves 5 ambiguous dev-v2b cases)

SA-H1, SA-H2, SEC-E1, SEC-H1, CO-H2 — all **passed in dev-v1, failed in
dev-v2b** on the same underlying data, all in the "dropped/under-argued
rubric point" class (correct retrieval, incomplete or mis-cited final
answer). This is the class RUNBOOK.md's own recommendation section flags
as *the* open problem — 11 of 12 dev-v2b failures are in this bucket and
neither prior design iteration targeted it with a high-confidence
mechanism. Existence-grounding's job here is narrower than fixing the
whole class: independently re-query (never reuse the original query) and
produce a definitive present/absent verdict for each claim component, so
Stage 4 can at least detect *incompleteness relative to what's
retrievable*, even though composing the complete answer is Stage 3's job
(deferred in v0). Golden detail for the ones with dev-answers.md entries:
CO-H2 (Art. 13(5)-(6) + Annex I Pt II duties), SA-H1 (SBOM capability,
zero current redundant coverage), SA-H2 (fan-out=45 top capability +
required "count ≠ criticality" critique). SEC-E1 golden: exactly 2 Controls
under `pol_incident_vulnerability_response_policy_9de859` —
`..._v1_manual` (implemented, overdue, review 2026-07-20) and `..._v2_automated`
(planned, no evidence) — dev-v2b's failure was omitting the overdue caveat
on the first. SEC-H1 golden: "MFA" → `cap_access_control_authentication_151816`,
exactly 7 obligations require it across CRA/GDPR(×2)/NIS2(×4, incl. the
explicit MFA obligation) — dev-v2b's failure was citing obligations by
description only, no real IDs.

---

## Build status

**All 26 tests pass, 20 of them against the live FalkorDB graph (not
mocked) — see "How to run" below.** Code lives in `pipeline/` (real logic)
and `tests/` (validation against the target cases above, not new/invented
questions, per the chosen build strategy).

| Component | Status | Notes |
|---|---|---|
| PROGRESS.md tracker | ✅ done | this file |
| Directory scaffold (`pipeline/`, `tests/`) | ✅ done | |
| Stage 1 — alias table (`pipeline/alias_table.py`) | ✅ validated | flags AU-M4, doesn't flag AU-H2/SEC-M2/SEC-M4 (non-regression) — `tests/test_stage1.py`, 2/2 pass |
| Stage 1 — entity-type extraction | ✅ validated | EM-E3 → "chain", correct |
| Stage 2 — structural classifier (`pipeline/structural.py`) | ⚠️ built, weak recall (documented, not hidden) | naive keyword heuristic catches 2/7 of the Miscount+Completeness spot-check IDs (SEC-M3's "how many", PM-H2's "each"). The other 5 (AU-M2, EM-H2, SA-H2, PM-H1, RM-H2) don't contain literal count/multi-part keywords despite being real Miscount/Completeness failures — expected, since README itself says these kinds are actually caught downstream at Stage 4/composition, not by Stage 2's question-text signal alone. `tests/test_stage2.py` asserts the *actual* (not aspirational) behavior so this gap stays visible, not silently papered over. |
| `pipeline/ps_client.py` (subprocess wrapper around `ps.py`) | ✅ done | reused, not reimplemented, per README |
| Stage 4 — rule check (`check_overdue_excludes_deprecated`) | ✅ validated | targets SEC-M2/SEC-M4. Ground truth pulled live: 6 Controls total, exactly 1 truly overdue (`ctrl_..._incident_vulnerability_response_policy_9de859_v1_manual`), 1 deprecated-but-past-due trap (`ctrl_..._legacy_asset_personnel_security_policy_7ed6c2_v1_manual`, review date 2026-01-01) — the exact shape SEC-M2/SEC-M4 fell into. Flags the trap, doesn't flag the correct set. |
| Stage 4 — scope-match, regulation-routing variant (`check_regulation_scope`) | ✅ validated for AU-H4 **and SA-H1** | confirmed live: `cap_security_logging_c4d9e2`'s full catalog has zero NIS2/GDPR rows — only CRA-1.0 and HELVEX-SOP-1.0 route through it (AU-H4). Same mechanism, different capability, reused unchanged for SA-H1: `cap_component_inventory_sbom_management_b5223c` is required only by CRA's SBOM obligation today, zero NIS2/GDPR redundant coverage — flags a claim of NIS2/GDPR coverage, doesn't flag the correct CRA-only claim. |
| Stage 4 — scope-match, SEC-H4 | ✅ validated — turned out to be `check_existence`, not a new mechanism | See "Design finding, revisited" below: the earlier "needs a bespoke redundancy mechanism" call was wrong. RUNBOOK.md's actual failure note ("listed duties verified by the v2/v3 controls, not failing on Aug 15") is an obligation-level over-claim, not a redundancy question — `check_existence` scoped to the failing control's own capability (`cap_data_encryption_0e50d3`, 5 real obligations) catches it directly. RUNBOOK-note-validated, not golden-validated (SEC-H4's golden text isn't in this repo). |
| Stage 4 — entity-type cross-check (`check_entity_type_match`) | ✅ validated | targets EM-E3 (chain vs control). EM-M4 not attempted — its granularity issue is a framing/bucketing mismatch, not a clean entity-type-noun mismatch; forcing it through this mechanism would be a false fit (see "Open questions") |
| Stage 4 — existence grounding (`check_existence`) | ✅ validated for SEC-E1, SEC-H1, **CO-H2, SEC-H4** | SEC-E1/SEC-H1: independent re-queries return exactly the golden set (2 controls; 7 obligations), live. CO-H2: independent re-query via `Requirement-[:SATISFIED_BY]->Obligation` over the 7 requirement IDs (Art 13.5/13.6, Annex I Pt II points 1/2/4, Art 13.8c for the CVD policy obligation — the graph maps that duty to 13.8c, not annex1_pt2_5 as `dev-answers.md`'s own provenance states, a golden/graph citation mismatch worth a catalog-maintenance note but not a blocker here) returns exactly the 7 golden obligation IDs. SA-H1/SA-H2 use different mechanisms (see their own rows) — not deferred anymore, all three of the prior session's gaps are closed. |
| Stage 4 — ranking grounding (`check_fanout_maximum`, **new**) | ✅ validated | targets SA-H2. Not existence grounding's shape (membership) — a claim of the form "X is required by the most obligations (N)" needs the ranking recomputed fresh, not looked up. Confirmed live: `cap_data_subject_rights_fulfilment_communication_8eedf0` is genuinely the maximum at 45, next-highest is 30 (`cap_binding_corporate_rules_governance_5d8a7a` / `cap_security_incident_reporting_449fa4`, tied). Flags a wrong capability/count claimed as the maximum, doesn't flag the correct one. |
| Three-block output composer (`pipeline/compose.py`, `pipeline/question_types.py`) | ✅ validated | wires Stage 1+2+4 end-to-end per question; see `tests/run_target_cases.py` (prints actual (A)/(B)/(C) JSON for 18 scenarios) and `tests/test_compose.py` (4/4 pass). Covers every target case with a *built* Stage 4 mechanism — SEC-M2, SEC-M4, AU-H4, EM-E3, SA-H1, SA-H2, CO-H2, SEC-H4 (failing + golden variant each) and SEC-E1, SEC-H1 (golden only) — deliberately excludes only AU-M4 now, a Stage 1 (not Stage 4) target case (composing it would be a vacuous fitness-gate pass, see the module docstring). Fixed along the way: `test_compose.py`'s golden-scenario assertion implicitly assumed every golden case gets the confident-type text — true by coincidence for the four original scenarios (all types B/C/D), false for SA-H2 (type G), which per README's Output posture gets the hedge even once its gate clears. Now asserts that explicitly instead of assuming it. |
| Stage 3 routing | ⬜ deferred (v0 scope) | route everything direct-answer for now, as planned |
| LLM judge ensemble | ⬜ deferred (v0 scope) | narrow residual, least urgent, as planned |
| Human escalation | ⬜ deferred (v0 scope) | stub: log + flag only, as planned |

### Composer result: no false auto-pass, across all mechanisms built so far

Running the 18 end-to-end scenarios (`tests/run_target_cases.py`) confirms
the pattern the success criteria require: all 8 `-failing` scenarios
(reproducing SEC-M2, SEC-M4, AU-H4, EM-E3, SA-H1, SA-H2, CO-H2, SEC-H4's
actual recorded transcript/RUNBOOK-note failures) compose into a "Fitness
gate failed" (A) and a `[FLAGGED -- not verified]` (B), never the
confident statement; all 10 `-golden`/correct scenarios get either the
confident statement or (SA-H2 only, type G) the hedge — never a flagged
output. All 18 are three-block-complete. Not yet built: (C)'s source_ref
provenance-chain rendering — no stage resolves those chains to citable
regulation text yet, documented as a gap in `pipeline/compose.py` rather
than faked.

### Design finding, revisited: SEC-H4 didn't need a new mechanism after all

The prior session's "Design finding" (below, kept for the record) treated
SEC-H4 as needing a bespoke redundancy-aware check because CRA, NIS2, and
GDPR each genuinely have *some* obligation requiring `cap_data_encryption`
— so a regulation-level routing check (AU-H4's shape) can't distinguish
the over-claim. That's true, but it was the wrong granularity to check at.
Pulling the actual RUNBOOK.md failure note this session ("listed duties
verified by the v2/v3 controls, not failing on Aug 15") shows the real
over-claim is at *obligation* granularity: the failing answer cited
MFA/logging obligations that require different capabilities entirely
(`cap_access_control_authentication`, `cap_security_logging`), not
`cap_data_encryption`. `check_existence`, scoped to the failing control's
own capability instead of the whole policy's capability set, catches this
with no new function — confirmed live and validated in
`tests/test_stage4.py::TestExistenceGroundingSECH4`. The general lesson:
"scope-match is not one mechanism" (original finding) still holds, but the
dividing line isn't "AU-H4-shape vs. something new" — it's "is the
independent query scoped to the right granularity," and existence
grounding was already generic enough to cover both once scoped correctly.

### Design finding this session: "scope-match" is not one mechanism, it's (at least) two

AU-H4 and SEC-H4 were grouped together in README.md as both being
scope-match/narrowing-discipline failures (rule 7). Building the checks
against live data shows they're not the same check with different
inputs:

- **AU-H4's shape (implemented as `check_regulation_scope`):** does the
  named regulation have *any* obligation that requires this capability at
  all? A pure routing/existence question over the catalog. Clean, binary,
  validated.
- **SEC-H4's shape (not yet implemented):** the Encryption-at-Rest
  control (`ctrl_..._data_protection_security_policy_8e4c18_v1_automated`,
  review date 2026-08-15 — confirmed this literally is the "August 15"
  control the question names) sits under a policy with **three** parallel
  Controls (v1/v2/v3), all `implemented`/`reviewed`, all backing the
  *same* obligations redundantly. Every regulation named in SEC-H4's
  question (CRA, NIS2, GDPR) genuinely does route through v1 — so a
  routing check like AU-H4's would never flag the real over-claim there.
  The actual failure is about **redundancy**: is an obligation solely
  backed by the one failing control, or does it have other currently-
  implemented backing that survives the failure? That's a different
  query shape (compare an obligation's full set of backing Controls
  against the one under test) and wasn't built this session — recorded
  here so it isn't silently forgotten or conflated with AU-H4's mechanism
  next time this is picked up.
- Also worth flagging: SEC-H4's actual golden text isn't available in
  this repo (held-out question, golden lives in the separate held-out
  repo per the held-out-set discipline) — RUNBOOK.md's failure note is
  the only grounding available here, which is enough to design the
  mechanism but not to write a tight must-flag/must-not-flag pair the way
  AU-H4's was validated. Treat any future SEC-H4 mechanism as
  RUNBOOK-note-validated, not golden-validated, unless the held-out repo
  becomes available.

**Superseded — see "Design finding, revisited" above.** This section's
"different query shape... wasn't built this session" call turned out to
be wrong once the actual RUNBOOK note was read closely: the over-claim is
obligation-granularity, not a redundancy question, and `check_existence`
(already generic) covers it once scoped to the right capability. Kept
here, not deleted, exactly per this section's own stated reason —
so the reasoning that led to the wrong call stays visible, not erased.

### How to run

```sh
cd spikes/compliance-decision-pipeline
/usr/bin/python3 -m unittest discover -s tests -p "test_*.py" -v

# to look at the actual composed (A)/(B)/(C) output, not just pass/fail:
/usr/bin/python3 tests/run_target_cases.py
```

Requires FalkorDB reachable at `localhost:6379`, graph `policy_system`
(same environment every other spike in this repo uses — no setup beyond
what's already running). Stage 1/2 tests have no graph dependency; Stage 4
and composer tests do.

**Next action:** (a) and (b) are both done now — SEC-H4, SA-H1, SA-H2, and
CO-H2 all have real, validated Stage 4 mechanisms (`check_existence`
reused twice with correctly-scoped queries, `check_regulation_scope`
reused once, and one genuinely new mechanism, `check_fanout_maximum`, for
SA-H2's ranking claim), and all four are wired into
`tests/run_target_cases.py`'s 18-scenario set with failing/golden pairs.
Every mechanism named in the README's Setup step 4 and Success Criteria
tables now has a built, validated implementation — nothing left stubbed
or deferred except the explicitly-v0-scoped items (Stage 3 routing, judge
ensemble, human escalation). What's left:
- Setup step 6's Stage 5 sampling-strategy dry-run (risk-weighted vs.
  uniform-random sample of the 108-transcript pool) — not yet done.
- The open pharma-auditor-acceptance-bar manual pass (below) — now
  unblocked, there's a full set of (A)/(B)/(C) outputs across all 8 target
  failure cases to run past that bar.
- A pass through the full Success Criteria table in README.md to check
  off each one explicitly against what's now built, rather than inferring
  it from this table.

---

## Open questions (not blocking, but don't lose track)

- **Pharma-auditor acceptance bar is unvalidated.** All grading so far
  (108 transcripts) used this project's own expert rubric, not a real or
  simulated pharma-auditor review. Plan: once Stage 1/2/4 mechanisms pass
  their target cases, do one explicit manual pass — a handful of
  pipeline outputs reviewed against "would this specific auditor sign off"
  — separate from the automated success criteria in README.md.
- EM-M4's entity-type mismatch is weaker/fuzzier than EM-E3's — may need a
  different mechanism than a clean entity-type cross-check, or may just be
  a partial-credit case. Don't force a fit; note honestly if it doesn't
  cleanly validate.
- ~~SEC-E1/SEC-H1 golden detail not yet pulled into this doc~~ — done (see
  "Target cases" above); resolved a prior session.
- CO-H2's requirement-to-obligation mapping surfaced a small
  golden/graph mismatch worth a catalog-maintenance note: `dev-answers.md`
  cites the coordinated-vulnerability-disclosure duty as Annex I Part II
  point (5); the graph maps that same obligation
  (`obl_maintain_a_coordinated_vulnerability_disclosure_policy_c182fe`) to
  Art 13.8c instead, and has no `annex1_pt2_5` requirement node at all.
  Doesn't block this pipeline (it re-derives from the graph, not the
  golden text), but worth fixing at the source so future graph-vs-golden
  cross-checks don't have to rediscover it.
