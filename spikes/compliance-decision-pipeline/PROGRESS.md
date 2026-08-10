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
  **Update (later session):** Stage 3's *routing-decision* layer (which
  path a question takes, and which Stage 4 check(s) become mandatory) was
  built and validated — see "Stage 3 — routing" below. What's still
  deferred is the actual decomposition/recursive-answer machinery the
  DECOMPOSE path would need to *execute*, because this pipeline has no
  answer-generation engine to decompose into (see README's "What This Is
  NOT" — it verifies a candidate answer, it doesn't produce one). The
  judge ensemble and human escalation remain fully deferred, unchanged.
- **Test oracle is the 162 already-graded question-instances, not new
  questions.** Iterate against the specific known target cases below
  (ground truth already established) before ever running a new/unseen
  question through the pipeline. New-question testing is phase 2, once
  mechanisms pass their own target cases — that's what tests
  generalization, not correctness.
- One open gap, not yet addressed: none of the 162 question-instances were
  graded against an actual pharma-auditor acceptance bar (they used this project's
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

**Update (later session): EM-M4 resolved, via a different mechanism than
originally planned here.** This session's own framing above ("spot-check
EM-M4" against `check_entity_type_match`) turned out to be the wrong tool
— see "Stage 4 — root-cause classification (EM-M4)" below for why, and
`fitness.py`'s module docstring for the finding this produced ("granularity
precision is not one mechanism, it's two"). Kept here unedited, not
rewritten, same discipline as every other superseded call in this doc.

### Stage 4 — root-cause classification (EM-M4, later session)

Revisits the "Open questions" entry that had stood since the prior
session: "EM-M4's entity-type mismatch is weaker/fuzzier than EM-E3's —
may need a different mechanism... Don't force a fit." That call was
correct at the time — no mechanism existed for the dimension EM-M4's
failure actually turns on. This session found one, by tracing RUNBOOK.md's
compressed failure note ("16/10 split framed differently than golden
(Clinical-draft 10 vs incident-v2 10)") back to real, named graph
entities instead of guessing at the golden's exact reasoning (which isn't
in this repo — held-out question):

- **"Clinical-draft"** = `cap_data_protection_impact_assessment_a51acb`,
  governed by `pol_clinical_data_integrity_policy_e1a539` — status
  `draft`, and its Standard has **zero** Controls at all. Nothing has
  been built; nothing has even been formally chartered beyond a draft
  policy. A pure governance gap.
- **"incident-v2"** = the three capabilities governed by
  `pol_incident_vulnerability_response_policy_9de859` (status `approved`):
  `cap_security_incident_reporting_449fa4`, `cap_incident_handling_4cf73e`,
  `cap_business_continuity_disaster_recovery_9c1c32`. This policy already
  has a **live** Control (`..._v1_manual`, `implemented`) — the chain is
  not stale — but *also* a second, parallel Control
  (`..._v2_automated`, `planned`) that hasn't landed yet. Something is
  actively being built, on a real (if incomplete) schedule — an
  engineering gap, not an organizational one.

Verified live, independently, not fit to the target numbers after the
fact: GDPR obligations requiring the Clinical capability = **10**; summed
across the three Incident-v2 capabilities = **10** — reproducing
RUNBOOK's own "Clinical-draft 10 vs incident-v2 10" note exactly.

This generalizes into a clean, four-category, independently-derivable
classification per capability (`classify_evidence_gap_root_cause`,
`pipeline/fitness.py`):

- **excluded** — Policy is `deprecated` (same exclusion discipline as the
  overdue rule check: it left the review cycle, it didn't fail it).
- **governance** — no live (`implemented`/`reviewed`) Control exists, and
  the Policy isn't deprecated. Includes both "Policy is `draft`" (Clinical)
  and "no Policy at all" (verified live: this is the majority case across
  GDPR's 42 linked capabilities — 32 of them have no Policy mapped at
  all) as the same category, not two — there's *even less* governance
  structure in the second case, not a different kind of gap.
- **engineering** — a live Control exists (chain not stale) but at least
  one other, non-deprecated Control under the same Policy is not yet live
  (e.g. `planned`) — an active, incomplete build.
- **resolved** — a live Control exists and nothing else is in progress.
  No evidence problem.

`check_evidence_gap_root_cause(capability_id, claimed_category)` cross-
checks a claim against this independent classification — same shape as
`check_entity_type_match` (does a stated category match ground truth?),
different dimension (root cause, not counting unit). Validated in
`tests/test_stage4.py::TestEvidenceGapRootCause` against all four
categories, including two non-regression cases neither of RUNBOOK's named
entities happens to cover (a fully `resolved` capability under the same
security policy as SEC-H4's; the `deprecated`/`excluded` legacy policy
SEC-M2/SEC-M4 already established as out-of-scope) — 9/9 pass, plus a
live re-derivation of the "10 and 10" counts themselves. Composed
end-to-end in `tests/run_target_cases.py` (`EM-M4-failing`/`EM-M4-golden`)
and locked in by two `test_compose.py` checks.

**Design finding, same pattern as scope-match's AU-H4/SEC-H4 split
(above): "granularity precision" is not one mechanism, it's (at least)
two.** EM-E3 is a counting-*unit* mismatch (chain vs. control) —
`check_entity_type_match`'s shape, a stated noun against Stage 1's
recorded entity-type. EM-M4 is a root-*cause* mismatch (governance vs.
engineering) — not reducible to a counting unit at all, hence the new,
separate mechanism. Full docstring and rationale in `pipeline/fitness.py`.

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

**All tests pass — see "How to run" below for the current count and
breakdown** (this line intentionally doesn't hardcode a number here
anymore; it drifted out of date at least twice already as the suite grew
session over session — one source of truth instead). Code lives in
`pipeline/` (real logic) and `tests/` (validation against the target
cases above, not new/invented questions, per the chosen build strategy).

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
| Stage 4 — entity-type cross-check (`check_entity_type_match`) | ✅ validated | targets EM-E3 (chain vs control) |
| Stage 4 — root-cause classification (`check_evidence_gap_root_cause`, **later session**) | ✅ validated | targets EM-M4 (governance vs engineering) — a different dimension than EM-E3's counting-unit mismatch, so a new mechanism, not a forced fit onto `check_entity_type_match`. See "Stage 4 — root-cause classification" above |
| Stage 4 — completeness grounding (`check_completeness`, **later session**) | ✅ validated, **not held-out** | targets CO-M2 (omission, not fabrication) — found via a live held-out audit, not a designed-in-advance target case. See "Live held-out generalization audit" and "CO-M2's gap closed" above. Explicitly non-blind: CO-M2 was used as direct design reference |
| Stage 4 — existence grounding (`check_existence`) | ✅ validated for SEC-E1, SEC-H1, **CO-H2, SEC-H4** | SEC-E1/SEC-H1: independent re-queries return exactly the golden set (2 controls; 7 obligations), live. CO-H2: independent re-query via `Requirement-[:SATISFIED_BY]->Obligation` over the 7 requirement IDs (Art 13.5/13.6, Annex I Pt II points 1/2/4, Art 13.8c for the CVD policy obligation — the graph maps that duty to 13.8c, not annex1_pt2_5 as `dev-answers.md`'s own provenance states, a golden/graph citation mismatch worth a catalog-maintenance note but not a blocker here) returns exactly the 7 golden obligation IDs. SA-H1/SA-H2 use different mechanisms (see their own rows) — not deferred anymore, all three of the prior session's gaps are closed. |
| Stage 4 — ranking grounding (`check_fanout_maximum`, **new**) | ✅ validated | targets SA-H2. Not existence grounding's shape (membership) — a claim of the form "X is required by the most obligations (N)" needs the ranking recomputed fresh, not looked up. Confirmed live: `cap_data_subject_rights_fulfilment_communication_8eedf0` is genuinely the maximum at 45, next-highest is 30 (`cap_binding_corporate_rules_governance_5d8a7a` / `cap_security_incident_reporting_449fa4`, tied). Flags a wrong capability/count claimed as the maximum, doesn't flag the correct one. |
| Three-block output composer (`pipeline/compose.py`, `pipeline/question_types.py`) | ✅ validated | wires Stage 1+2+4 end-to-end per question; see `tests/run_target_cases.py` (prints actual (A)/(B)/(C) JSON for 18 scenarios) and `tests/test_compose.py` (4/4 pass). Covers every target case with a *built* Stage 4 mechanism — SEC-M2, SEC-M4, AU-H4, EM-E3, SA-H1, SA-H2, CO-H2, SEC-H4 (failing + golden variant each) and SEC-E1, SEC-H1 (golden only) — deliberately excludes only AU-M4 now, a Stage 1 (not Stage 4) target case (composing it would be a vacuous fitness-gate pass, see the module docstring). Fixed along the way: `test_compose.py`'s golden-scenario assertion implicitly assumed every golden case gets the confident-type text — true by coincidence for the four original scenarios (all types B/C/D), false for SA-H2 (type G), which per README's Output posture gets the hedge even once its gate clears. Now asserts that explicitly instead of assuming it. |
| Stage 3 — routing decision (`pipeline/routing.py`) | ✅ validated (decision layer only) | `route_question()` maps Stage 1/2 signals + question type to a `RoutingDecision` (path + mandatory Stage 4 check names), and `compose.py` now enforces it — closes the vacuous-pass gap `tests/run_target_cases.py` previously worked around by excluding AU-M4. Actual decomposition/recursive-answer execution remains deferred — see "Stage 3 — routing" below and the "Chosen build strategy" update above |
| LLM judge ensemble | ⬜ deferred (v0 scope) | narrow residual, least urgent, as planned |
| Human escalation | ⬜ deferred (v0 scope) | stub: log + flag only, as planned |
| Stage 5 — sampling rule (`pipeline/stage5_sampling.py`) | ✅ dry-run validated | Setup step 6, done this session — see "Stage 5 sampling dry-run" below. Real logic (risk classifier reusing Stage 1/2 + a sampler), not a stub; audit/classify/promote/regression-check/version (Stage 5 steps 2-6) remain out of v0 scope, process not code |
| (C)'s `source_ref` provenance rendering (`pipeline/provenance.py`) | ✅ validated | Done this session — the acceptance-bar pass's concrete finding, fixed same session. Scoped to Obligation (one hop via `SATISFIED_BY`) and Requirement (direct) ids only — Control/Capability ids deliberately excluded, confirmed live that a full transitive walk for those returns dozens of unrelated regulation-article rows (see "Pharma-auditor-acceptance-bar manual pass" below). Validated in `tests/test_provenance.py` (5/5) and wired into `compose.py`'s (C), locked in by two new `test_compose.py` checks |

### Stage 3 — routing (later session)

Built `pipeline/routing.py`'s `route_question(stage1, stage2, question_type)
-> RoutingDecision`, wired into `compose.py` (optional `routing` param,
backward compatible) and into `tests/run_target_cases.py`'s `_run` helper.
Scope, deliberately bounded: this is the routing *decision* (which of
README's four paths a question takes, and — for the direct-answer paths —
which Stage 4 check(s) become mandatory), not decomposition/recursive-
answer execution. This pipeline verifies a candidate answer; it has no
answer-generation engine to decompose *into* (README's "What This Is
NOT"), so DECOMPOSE stays a classification, same as it was before Stage 3
existed via the type-F/G hedge path in `compose.py` — Stage 3 makes that
classification an explicit, testable decision instead of an implicit one.

**The concrete gap this closes:** `FitnessResult.passed` is vacuously
`True` when zero checks were run. `tests/run_target_cases.py` already
documented this concretely — it excluded AU-M4 specifically because
composing it with no Stage 4 mechanism behind it would have been a silent
vacuous pass, and the file said so in its own docstring rather than hide
it. `route_question` now assigns AU-M4 (type B, "stale" disambiguation) a
mandatory check name (`stale_chain_strict_reading`) that doesn't exist in
`fitness.py` yet, and `compose.py` enforces that at least one mandatory
check_name actually appears among the checks performed before allowing a
confident result. AU-M4 is now included in `run_target_cases.py`
(`AU-M4-unbuilt-check`) specifically to demonstrate this: it composes with
zero Stage 4 checks and correctly comes back `gate_passed: false`,
`[FLAGGED -- not verified]`, naming the missing mechanism by name —
instead of, as it would have before this session, silently passing on an
empty check list. Validated in `tests/test_compose.py`
(`test_mandatory_check_not_performed_fails_closed_not_vacuously`).

**Design resolution: multi_part vs. a type's own known trigger.** README's
routing bullets list "type with a known specific trigger (B, C, D)" and
"needs decomposition (multi-part, ...)" without stating which wins when
both fire on the same question — not hypothetical: SEC-M4 is Stage 2
`multi_part=True` (its "which...and which" phrasing trips the keyword
heuristic) *and* a validated, already-shipped type-B direct-answer target
case (Stage 4's rule check, prior session). Decomposing it would
contradict an already-built, already-validated mechanism. Resolved: a
type's own known-trigger mechanism wins over the multi_part structural
signal, because the mechanism already reduces the compound claim to one
verifiable derived quantity (SEC-M4's rule check evaluates the whole
"overdue set" as a single query result — there's nothing left for
decomposition to do). multi_part only drives DECOMPOSE when the type
itself has no known trigger to lean on. Full reasoning in
`pipeline/routing.py`'s module docstring; locked in by
`tests/test_routing.py::test_sec_m4_multi_part_still_routes_direct_not_decompose`,
which asserts the premise (`stage2.multi_part` is actually `True` for
SEC-M4's real text) before asserting the resolution — not a synthetic
example.

**Mandatory-check mapping built and validated per type/signal** (any-of
semantics — at least one named check must have run, not all; see
`routing.py` for why v0 has no textual discriminator to require a
*specific* one for B/D's grounding shapes):

| Type / signal | Mandatory check(s) | Validated against |
|---|---|---|
| A, E, H | none (near-100% reliability or solved gap-check) | definitional, no target case exercises this directly |
| B, disambiguation term = overdue/deprecated | `rule_overdue_excludes_deprecated` | SEC-M2, SEC-M4 (incl. the multi_part resolution above) |
| B, disambiguation term = stale | `stale_chain_strict_reading` (not built yet — intentional, visible gap) | AU-M4 |
| B, no disambiguation term | any of `existence_grounding` / `scope_match_regulation_routing` / `fanout_maximum` | SEC-E1, SEC-H1, SA-H1, CO-H2 |
| C, entity_type recorded | `entity_type_cross_check` | EM-E3 |
| C, no entity_type recorded | none (tool-computed-count check doesn't exist yet — matches the Success Criteria table's existing "Miscount elimination: NOT YET VERIFIABLE") | EM-M4 has its own real, validated Stage 4 mechanism now (`evidence_gap_root_cause`, added later session) — but no question-text signal exists yet for Stage 3 to auto-require it, so it stays an available check a caller selects, same as most Stage 4 mechanisms already work in `run_target_cases.py` |
| D, "if X breaks/fails" shape | any of `existence_grounding` / `scope_match_regulation_routing` / `fanout_maximum` | AU-H4, SEC-H4 |
| D, not that shape | none (documented gap, same reasoning as C) | AU-H2 (non-regression: must not spuriously require the grounding set) |

Re-ran every one of the 18 previously-built end-to-end scenarios through
this enforcement as a regression check (same must-flag/must-not-flag
discipline as every other mechanism here) — all 18 already carried a
check_name that satisfies their type's mandatory set, so none of them
newly fail closed. Only AU-M4 (newly added, not previously composed)
demonstrates the enforcement actually firing.

**Not attempted, and not silently glossed over:** distinguishing *which*
specific grounding check a no-disambiguation type-B or hypothetical-chain
type-D question needs (existence vs. scope-match vs. fanout) from question
text alone — no target case currently motivates a specific discriminator,
and inventing one without validation would repeat the exact mistake this
pipeline's own discipline exists to avoid (see EM-M4's "don't force a fit"
precedent). The any-of set is the honest current precision; tightening it
is future work, same bar as promoting `stale_chain_strict_reading` from a
named gap to a real Stage 4 mechanism.

### Composer result: no false auto-pass, across all mechanisms built so far

Running the 27 end-to-end scenarios (`tests/run_target_cases.py`, updated
across five later sessions to include Stage 3's routing, the AU-M4
enforcement demonstration, EM-M4's root-cause classification, CO-M2's
completeness grounding, the AU-H2 vacuous-pass regression scenario, and
the SA-E3/SA-M2/PM-M3 precision tests) confirms the pattern the success
criteria require: all 10 `-failing` scenarios (reproducing SEC-M2, SEC-M4,
AU-H4, EM-E3, SA-H1, SA-H2, CO-H2, SEC-H4, EM-M4, CO-M2's actual recorded
transcript/RUNBOOK-note failures) compose into a "Fitness gate failed" (A)
and a `[FLAGGED -- not verified]` (B), never the confident statement; all
15 `-golden`/correct scenarios get either the confident statement or
(SA-H2 only, type G) the hedge — never a flagged output; the remaining 2
(`AU-M4-unbuilt-check`, `AU-H2-zero-checks`) are neither `-failing` nor
`-golden` reproductions but Stage 3 enforcement demonstrations (see
"Stage 3 — routing" and the "Overfitting-fix pass" section above) — also
correctly flagged, for a different reason each time (no mandatory check
performed / no check performed at all, not a check that failed). All 27
are three-block-complete. (C)'s `source_ref`
provenance-chain rendering was not yet built as of this paragraph's
original writing — since fixed, see "Pharma-auditor-acceptance-bar manual
pass" below for the update and `pipeline/provenance.py` for the
implementation. Kept here as the original record, not rewritten.

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

### Stage 5 sampling dry-run (this session — README Setup step 6)

**Pool:** 162 question-instances (54 dev questions × 2 `cli-tool-semantics`
runs + 54 held-out questions × 1 `skill-transfer` raw-Cypher run), 33 of
them known failures — transcribed verbatim from both RUNBOOKs'
per-question result tables into `tests/fixtures_stage5.py`, cross-checked
against README's own "162 question-instances" / "33 failure instances"
tallies and each RUNBOOK's own pass-count arithmetic (43/54, 42/54, 44/54 ⇒
11 + 12 + 10 = 33).

**Documentation discrepancy found and recorded, not silently fixed:** this
doc's own "Next action" section (below) and README's Deliverables section
both call this "the 108 already-graded transcripts" — 108 is only
`cli-tool-semantics`'s two runs (54×2); it omits the 54 held-out instances
named in the same sentence. The dry-run uses the arithmetically-correct
162, matching README's type-reliability table. Worth a one-line fix in
README/PROGRESS wording later; not blocking.

**Risk classifier** (`pipeline/stage5_sampling.classify_risk`): reuses the
already-validated Stage 1 (`needs_disambiguation_check`) and Stage 2
(`multi_part`) classifiers unchanged, plus one new heuristic,
`_is_comparison_shaped` — a keyword/regulation-name-count check standing in
for README's "comparison/relation-shaped claims" signal, which has no
built classifier anywhere else in the pipeline. Labeled everywhere as a
dry-run-only heuristic, not a Stage 1/2/4 mechanism — do not promote it to
`pipeline/` proper without giving it the same must-flag/must-not-flag
validation discipline the real mechanisms got.

**Result** (`tests/stage5_dry_run.py`, 2000 trials/sampler/sample-size,
seed `20260809`):

| Sample size | Risk-weighted mean hit-fraction | Uniform-random mean hit-fraction | Risk-weighted wins? |
|---|---|---|---|
| 20 (~12% of pool) | 0.227 | 0.203 | ✅ |
| 30 (~18% of pool) | 0.240 | 0.202 | ✅ |
| 45 (~28% of pool) | 0.250 | 0.202 | ✅ |

Risk-weighted beats uniform-random at every tested size — the widening gap
as sample size grows tracks the risk pool's own size (57 of 162 instances
carry at least one of the three flags; uniform-random's fraction stays
essentially flat at ~20%, the pool's base failure rate, exactly as
expected for an unweighted draw). **Success criterion "Sampling
efficiency" — PASS.** Composition of the risk pool: 13 instances flagged
by Stage 1 disambiguation, 26 by Stage 2 multi-part, 31 by the comparison
heuristic (some overlap; 57 total unique). Full numbers reproducible via
`tests/stage5_dry_run.py`; fast deterministic sanity checks (not the full
Monte Carlo) live in `tests/test_stage5.py` (8/8 pass, no FalkorDB
dependency).

**Caveat carried over from README's own "What This Is NOT":** this proves
the *sampling rule* beats chance at surfacing *already-known* failures. It
cannot and does not test whether a live Stage 5 audit would catch a
genuinely new, unhypothesized failure kind — every failure in this pool is
by construction already known. That harder question needs a real Stage 5
cycle against live, previously-unseen pipeline output, later work per
README.

### Pharma-auditor-acceptance-bar manual pass (this session)

Reviewed all 18 composed `tests/run_target_cases.py` outputs against "would
a pharma-industry auditor actually sign off on this" — the open question
PROGRESS.md flagged as unblocked once every target case had a real Stage 4
mechanism behind it (see "Open questions" below, now resolved).

**What holds up:** the core discipline an auditor would check first —
never present an unverified or wrong claim as confident — held on all 18.
Every one of the 8 `-failing` scenarios (reproductions of SEC-M2, SEC-M4,
AU-H4, EM-E3, SA-H1, SA-H2, CO-H2, SEC-H4's actual recorded failures) comes
back `gate_passed: false` with a `[FLAGGED -- not verified]` (B) and an (A)
that names the specific contradicting mechanism and evidence — never a
generic hedge. Every `check_name` in (C)'s `fitness_checks` is a named,
inspectable mechanism (`rule_overdue_excludes_deprecated`,
`scope_match_regulation_routing`, `entity_type_cross_check`,
`existence_grounding`, `fanout_maximum`) — an auditor doesn't have to trust
a black-box verdict, they can see exactly which rule fired and why. All 18
are three-block-complete, confirmed programmatically.

**What would block an actual sign-off:** (C) currently carries the
structured graph values (real IDs, counts, routed regulations) but not the
other half of what README's own Output-posture section promises —
`source_ref` provenance chains back to the actual regulation article text.
An auditor reviewing, say, SEC-H4-golden's claim ("GDPR Art 32(1)(a) is the
primary casualty") can see from (C) that
`obl_apply_pseudonymisation_and_encryption_as_controller_fc1f7e` is a real,
independently-reconfirmed obligation ID — but nothing in (C) shows *that
this specific ID is what "Art 32(1)(a)" means*, or quotes the article
text itself. They'd have to go open `docs/regulations/GDPR.md` by hand for
every claim, which is exactly the "citation alone" failure mode (C) was
designed to make unnecessary. This is not a new finding — PROGRESS.md's
Build status table already flags "(C)'s source_ref provenance-chain
rendering -- no stage resolves those chains to citable regulation text
yet" — this pass confirms it concretely, against real output, as the
single biggest remaining gap between "mechanically verified" and
"auditor-signable."

**Secondary note, not a blocker:** the (B) answer text in all 18 scenarios
is test-harness prose reproducing (or correcting) an actual transcript
answer for mechanism validation, not natural-language output from a live
answer-composition step (Stage 3, deferred). Tone/register/phrasing
quality — a real part of what a human auditor judges — hasn't been
reviewed yet because there's no live composer to produce it. Re-run this
acceptance-bar pass once Stage 3 exists and generates real prose; today's
pass validates the gate mechanism and the three-block contract, not final
answer prose.

**Verdict (as of this pass):** the gate discipline (never auto-pass a
wrong answer, always name the mechanism) is auditor-grade already. The
evidence-citation discipline is half-built (structured values: yes;
regulation-text provenance: not yet). Recommend building the `source_ref`
rendering next, before claiming full AD-7 sign-off — the graph data it
needs (`source_ref` fields on Requirement/Obligation nodes) already exists
and is already used for citation in the `cli-tool-semantics` transcripts
(RUNBOOK.md, throughout); it's a rendering gap in `compose.py`, not a data
gap.

**Update, same session — the `source_ref` gap above is now closed.**
Built `pipeline/provenance.py` and wired it into `compose.py`'s (C). Scope
resolved live before wiring it in, not assumed: a full transitive walk
back to Regulation for a Control or Capability id returns dozens of
unrelated regulation-article rows (tested live against SEC-E1's control —
31 rows across CRA/NIS2/GDPR, because the Standard it sits under governs a
Capability shared by many other Obligations) — rendering that as "this
Control's provenance" would reintroduce, one level up, the exact
over-citation problem `check_regulation_scope`'s narrowing discipline
(SKILL.md rule 7) exists to catch. So the fix is scoped to exactly what
SKILL.md's own Provenance rule (rule 6) describes: Requirement ids resolve
directly (their own `EXPRESSES` edge carries `source_ref`); Obligation ids
resolve one hop back via `SATISFIED_BY` to the Requirement(s) that satisfy
them. Control/Capability ids are deliberately left unresolved — not a gap,
a design boundary confirmed against live data. Validated against target
cases in `tests/test_provenance.py` (5/5 pass): SEC-H1's NIS2-only MFA
obligation resolves to exactly `NIS2-1.0` / `Art. 21(2), point (j)`;
CO-H2's fabricated `obl_does_not_exist_deadbeef` resolves to `[]` (honest
"not found," not an error) — and, as a bonus confirmation, CO-H2's real
coordinated-vulnerability-disclosure obligation resolves to
`CRA-1.0_req_art_13.8c`, live evidence matching the golden/graph citation
mismatch this doc already flagged (dev-answers.md cites Annex I Pt II
point (5) instead). `test_compose.py` gained two integration tests
locking in that SEC-H1-golden's (C) now carries resolved source_refs and
SEC-M2-failing's (C) correctly carries none (Control-only claim). All 41
tests pass (39 + 2). The acceptance-bar verdict above is superseded by
this: evidence-citation discipline is now fully built for the id types
that carry a citable source, not just structured values — kept above, not
deleted, so the reasoning that led to flagging the gap stays visible.

### Success Criteria checkoff (README.md's table, this session)

| Criterion | Verdict | Basis |
|---|---|---|
| Vocabulary-gap precision | ✅ PASS | `test_stage1.py`: flags AU-M4, doesn't false-flag AU-H2/SEC-M2/SEC-M4 |
| Interaction-rule coverage | ✅ PASS | `test_stage4.py::TestRuleCheckOverdueExcludesDeprecated`: flags SEC-M2/SEC-M4's deprecated-inclusion trap, Stage 1 correctly silent on it |
| Scope-match precision | ✅ PASS | `check_regulation_scope` (AU-H4, SA-H1) + `check_existence` scoped to the failing control's own capability (SEC-H4) — both flag the over-claim, both pass the correctly-scoped golden, confirmed live in `run_target_cases.py` |
| Granularity precision | ✅ PASS (upgraded, later session) | EM-E3 validated (`test_stage4.py::TestEntityTypeCrossCheck`, counting-unit mismatch). EM-M4 previously PARTIAL — resolved once its actual dimension (root-cause, not counting-unit) was identified: `check_evidence_gap_root_cause`, validated in `TestEvidenceGapRootCause` (9/9), including an independent live re-derivation of RUNBOOK's own "10 and 10" figures. See "Stage 4 — root-cause classification" above |
| Ambiguity resolution | ✅ PASS | All 5 (SA-H1, SA-H2, SEC-E1, SEC-H1, CO-H2) get a definitive live-graph verdict via `check_existence`/`check_regulation_scope`/`check_fanout_maximum` |
| Miscount elimination | ⬜ NOT YET VERIFIABLE | Stage 2's count-shaped flag exists but has documented weak recall (2/7 spot-check), and there's no live answer-composition step to actually enforce "tool-computed number reaches the final answer" against — nothing to pass or fail this against yet, distinct from a fail. Stage 3's routing decision layer (later session) doesn't change this: it enforces which check ran, not that a specific number was tool-computed, since no such check exists yet (see `pipeline/routing.py`) |
| Judge ensemble reliability | ❌ NOT BUILT | Explicitly deferred v0 scope, per plan |
| No false auto-pass | ✅ PASS (strengthened) | All 10 `-failing` scenarios return `gate_passed: false`; zero false auto-passes across every mechanism built. Stage 3 (later session) closed a real edge of this: `FitnessResult.passed` was vacuously `True` on zero checks run — `AU-M4-unbuilt-check` now demonstrates the fix, see "Stage 3 — routing" above. CO-M2 (later session) closed a different edge: an incomplete-but-non-fabricated claim, which `check_existence` alone can't see — see "CO-M2's gap closed" above |
| Auditability | ✅ PASS (strengthened) | Every fitness check carries a named `check_name`, never an unlabeled aggregate boolean. (C) now also carries Stage 3's own routing decision (path + mandatory check names + reason), not just Stage 4's checks — an auditor can see *why* a check was mandatory, not only whether one ran |
| Sampling efficiency | ✅ PASS | This session's Stage 5 dry-run: risk-weighted beats uniform-random at all 3 tested sample sizes, 2000 trials each (see above) |
| Three-block completeness | ✅ PASS | `run_target_cases.py`: 27/27 three-block-complete |
| Block (C) sufficiency for relational claims | ✅ PASS (updated, same session) | The relational over-claim itself (AU-H4/SA-H1/SEC-H4) is directly catchable from (C)'s side-by-side claimed-vs-routed/retrieved sets, no re-derivation needed. `source_ref`-to-article-text rendering (`pipeline/provenance.py`) is now built and wired in for every Obligation/Requirement id in (C) — see "Pharma-auditor-acceptance-bar manual pass" above for the update that closed this |

**Net:** 10 of 12 criteria fully pass (upgraded from 9 across the two
later sessions: Stage 3's mandatory-check enforcement strengthened two
already-passing criteria, and EM-M4's root-cause mechanism moved
Granularity precision from partial to full pass). 1 isn't yet testable (no
live answer path to check "miscount elimination" against), 1 is out of v0
scope by design (judge ensemble). Nothing here contradicts the "no false
auto-pass" bar, which is the one criterion this spike cannot compromise on.

### How to run

```sh
cd spikes/compliance-decision-pipeline
/usr/bin/python3 -m unittest discover -s tests -p "test_*.py" -v

# to look at the actual composed (A)/(B)/(C) output, not just pass/fail:
/usr/bin/python3 tests/run_target_cases.py

# Stage 5's sampling-rule dry-run (risk-weighted vs. uniform-random):
/usr/bin/python3 tests/stage5_dry_run.py
```

Requires FalkorDB reachable at `localhost:6379`, graph `policy_system`
(same environment every other spike in this repo uses — no setup beyond
what's already running). Stage 1/2/3 tests have no graph dependency; Stage
4 and composer tests do. Stage 5's sampling tests/dry-run have no graph
dependency either (pure question-text classification + in-memory sampling).

**All 74 tests pass** — `test_compose.py` 13, `test_provenance.py` 5,
`test_routing.py` 14, `test_stage1.py` 2, `test_stage2.py` 2,
`test_stage4.py` 30, `test_stage5.py` 8. Growth this doc's sessions:
Stage 3's routing layer (+13 `test_routing.py`, +2 compose-level), EM-M4's
root-cause mechanism (+9 `TestEvidenceGapRootCause`, +2 compose-level),
CO-M2's completeness mechanism (+4 `TestCompletenessGrounding`, +2
compose-level), the overfitting-fix pass (+1 hypothetical-chain-detection
test, +1 AU-H2 vacuous-pass regression test) — on top of the 41 from the
session before Stage 3. 48 of them against the live FalkorDB graph.

**Next action:** every item from the previous session's "what's left" list
that was still open is now done:
- **Stage 3's routing-decision layer** — done, see "Stage 3 — routing"
  above. Scope note, not a partial completion: the *decision* (which path,
  which mandatory check) is fully built and validated; actual
  decomposition/recursive-answer *execution* remains out of scope because
  no answer-generation engine exists in this pipeline to decompose into
  (unchanged from the original v0 call — see "Chosen build strategy"'s
  update at the top of this doc). The acceptance-bar re-run against real
  generated prose (previous session's stated reason Stage 3 was needed
  first) still needs that answer-generation engine, which this session
  did not build — carried forward below, not resolved.
- **EM-M4's Granularity precision gap** — done, see "Stage 4 — root-cause
  classification" above. Not the mechanism originally guessed at
  (`check_entity_type_match`) — a new one (`check_evidence_gap_root_cause`),
  found by tracing RUNBOOK.md's failure note back to real graph entities
  rather than forcing the existing mechanism to fit. Success Criteria
  table's Granularity precision moves from PARTIAL to PASS.
- **Live held-out generalization audit** — done, see "Live held-out
  generalization audit" below. First real (not dry-run) test of whether
  this pipeline generalizes beyond its own design cases: 0 of 3
  previously-unseen held-out failures (CO-M2, CO-M4, PM-H3) caught.
  CO-M2's specific gap (`check_existence` blind to omission) closed same
  session with `check_completeness` — see "CO-M2's gap closed" below.
  CO-M4 and PM-H3 remain open, honestly scoped, not forced.
- **Overfitting-fix pass** — done, see "Overfitting-fix pass" above.
  Closed a live "no false auto-pass" violation (AU-H2 composing a
  confident answer on zero Stage 4 checks — more severe than any prior
  finding, since it broke the pipeline's one non-negotiable guarantee),
  broadened and re-validated the hypothetical-chain regex, audited and
  fixed a silently-paraphrased target case (SEC-H1, now verbatim text +
  correct type D), and confirmed Stage 1's alias table has no ungrounded
  gap to fill. SEC-E1's own paraphrase surfaced a second, smaller,
  not-yet-fixed gap (status verification) — carried forward below.
- LLM judge ensemble and human escalation — still deferred, unchanged.

What's left:
1. **An actual answer-composition/generation path** — needed before the
   acceptance-bar pass can be re-run against real generated prose instead
   of `run_target_cases.py`'s test-harness reproductions, and before
   Stage 3's DECOMPOSE path can do more than classify (today it correctly
   identifies "this needs decomposition" but has nothing to recursively
   route sub-questions *to*). This is a larger scope decision than the
   routing layer was — flagging it explicitly rather than deferring
   silently, since it's the one piece of README's design this pipeline
   still cannot demonstrate end-to-end.
2. **LLM judge ensemble** and **human escalation** — deferred v0 scope,
   least urgent, per the original plan.
3. Two check-name gaps Stage 3 made newly visible, not new problems: no
   Stage 4 mechanism exists yet for `stale_chain_strict_reading` (AU-M4's
   mandatory check — see "Stage 3 — routing" above) or for a
   tool-computed-count check (type C's "Miscount elimination" criterion,
   already NOT YET VERIFIABLE in the Success Criteria table below, unchanged
   by this session). Building either would follow the exact same
   must-flag/must-not-flag discipline as every mechanism in `fitness.py`.
4. **PM-H3 and CO-M4**, from the live held-out audit — genuinely new
   mechanism shapes, not variants of anything built: PM-H3 needs a
   counterfactual/what-if impact-ranking check (no existing mechanism
   shape fits); CO-M4 needs Stage 2/3 multi-claim decomposition
   (recognizing a question requires *two* independently-checkable claimed
   sets), not a new Stage 4 check per se. See "CO-M2's gap closed" above
   for the scoping reasoning. Both are known, not attempted, not silently
   dropped.
5. **SEC-E1's status-verification gap** (overfitting-fix pass, above):
   `check_existence` confirms Control *ids* but never their
   `implementation_status`, so the "and what state is each in?" half of
   the real dev-questions.md question is unverified. Needs a signature
   change (claimed (id, attribute) pairs, not just ids) to
   `check_existence`/`check_completeness` — real work, not a quick patch,
   and has no validated target case of its own yet.
6. **Promote `_is_comparison_shaped` from Stage 5's dry-run heuristic to a
   real Stage 2 signal** (overfitting-fix pass, above) — the one
   principled opportunity found among Stage 2's 5 documented spot-check
   misses (RM-H2's "benchmark X against Y" shape). Already flagged in
   `stage5_sampling.py` as needing the same must-flag/must-not-flag
   validation discipline as every real mechanism before promotion; not
   done this session so as not to rush an unvalidated heuristic into a
   live signal path.
7. One small, explicitly-non-blocking note carried forward: CO-H2's
   golden/graph citation mismatch (Annex I Pt II point 5 vs. Art 13.8c) is
   a catalog-maintenance item for the source data — still not a pipeline
   bug. (EM-M4's entity-type/granularity gap, the other item previously
   listed here, is resolved — see "Open questions" below and "Stage 4 —
   root-cause classification" above.)

---

## Live held-out generalization audit (later session — a real Stage 5 cycle, not the dry-run)

Prompted by a direct question: can this pipeline's non-overfitting be
*proven*, not just argued? The honest starting point was no — every Stage
4 mechanism was built by looking at one specific known failure and writing
code that catches exactly that failure; design set and validation set were
the same 33 known failures throughout. This section is the actual test,
not another argument.

**What "held-out" still means here, precisely.** Of `blind_questions.tsv`'s
54 questions, this pipeline's mechanisms were built against exactly 7 of
its 10 recorded failures (AU-M4, SEC-M2, SEC-M4, AU-H4, SEC-H4, EM-E3,
EM-M4). **Three failures were never touched by any design or validation
step: CO-M2, CO-M4, PM-H3.** Their existence was known (README's
failure-kind taxonomy was derived by reading all 33 failures, including
these three) but no mechanism was ever hand-fit to them specifically. That
makes them the closest thing to genuine held-out data this repo still has
for this pipeline — used here for the first time, as a blind evaluation
of the *already-built* pipeline, not as design input.

### Result: 0 of 3 previously-unseen failures caught

| ID | Question | RUNBOOK failure | Caught by any existing mechanism? |
|---|---|---|---|
| CO-M2 | "Which of our extracted regulatory duties have the shakiest provenance confidence and should get a human review?" | Golden: 24 obligations (3 at `confidence=0.75` + 21 at `confidence=0.80`, verified live). Agent found 15 (missed 9 of the 0.80 band). | **No.** Ran the real 24-item golden set and the real 15-item failing set through the unmodified `check_existence` — it correctly does not flag the golden claim, but also does not flag the incomplete one. `check_existence` only checks `claimed - retrieved` (fabrication/over-claim); it never checks `retrieved - claimed` (omission/under-claim). This is a Completeness failure, and the design doc's own failure-kind table already says Completeness needs Stage 2 decomposition + a composed-answer fitness gate — neither is live. The gap is exactly where README predicted it, not a surprise, but this is the first time it was demonstrated against real data instead of asserted. |
| PM-H3 | "Which policy state changes would unblock the most GDPR evidence chains?" | Golden lever 1 (16 Legacy chains) correct; lever 2 wrong — agent named "Clinical draft→approved" instead of "Incident v2 draft-standard/planned-control" (6 Art. 33 reqs × dual paths). | **No.** This needs a *counterfactual* query ("if Policy/Standard X's state changed, how many chains newly become live") — a different computation from anything built. `check_evidence_gap_root_cause` (built for EM-M4) classifies a capability's *current* state and, notably, uses the same two policies (Clinical, Incident) this question turns on — but classifying current state isn't the same as ranking hypothetical state changes by impact. Related subject matter, not a covering mechanism. |
| CO-M4 | "Where do NIS2's minimum security measures overlap with CRA's essential requirements and GDPR's security-of-processing rules?" | Overlap mapping correct at capability level; omitted the rubric-required non-overlap enumeration. | **No.** "What the rubric additionally requires you to enumerate" isn't derivable from the graph at all — it's a completeness-of-*response-shape* requirement external to any structured re-query. Same failure kind as CO-M2 (Completeness), same reason it's uncaught. |

### One precision check on the same previously-untouched data

`RM-H4` (passing, held-out, never used to build or validate anything):
golden correctly reports exactly 1 truly-overdue control, explicitly
excluding a second, deprecated one it's aware of. Fed the golden-shaped
claim through the unmodified `check_overdue_excludes_deprecated` — correctly
not flagged. Weaker evidence than the failures above (same claim shape as
the existing SEC-M2/SEC-M4 golden cases, just a different question wrapped
around it), but consistent, not contradictory.

### What this settles and what it doesn't

- **It settles the original question with a number, not an argument: 0/3.**
  Every previously-unseen held-out failure this pipeline had never looked
  at slipped through uncaught. If these three had shown up in live output
  today, the pipeline would have marked all three "verified" or let them
  pass Stage 4 clean — the exact failure mode Stage 5 exists to catch
  later, now demonstrated firsthand rather than assumed.
- **It does not mean the pipeline is worthless or that Stage 4's actual
  mechanisms are fake.** The earlier stress test (novel capabilities/
  policies never used in any design step, run through `check_regulation_
  scope`, `check_existence`, `check_evidence_gap_root_cause`) showed those
  *specific, already-built* mechanisms hold up on fresh graph entities
  within their own domain. What doesn't hold up is *coverage* — the set of
  failure *kinds* this pipeline can catch at all is bounded by the 7 kinds
  it was explicitly built against, and Completeness (2 of the 3 misses
  here) is a kind with a known, already-documented, not-yet-built fix path
  (decomposition + composed-answer gate).
- **These three cases are now spent.** Any future mechanism built to catch
  Completeness-shaped failures using CO-M2/CO-M4 as design input can no
  longer treat them as held-out validation for that mechanism — same
  discipline as every other target case in this doc. A genuinely fresh
  held-out set (new questions, newly graded, never read during design)
  would be needed to validate a completeness-checking fix, if one gets
  built. `blind_questions.tsv`'s remaining 44 passing instances are still
  usable as precision (false-positive) checks — they were not used to
  design anything — but not as recall (does-it-catch-new-failures) checks,
  since none of them are known failures.

### CO-M2's gap closed (same session, explicitly non-blind)

User decision after the audit above: fix the CO-M2 gap now, using CO-M2 as
design reference, with the cost (CO-M2 no longer usable as held-out data)
accepted explicitly rather than silently.

Built `check_completeness(claimed_ids, independent_query, id_column_index)`
in `pipeline/fitness.py` — the other direction from `check_existence`.
Where `check_existence` flags `claimed - retrieved` (a claimed id that
doesn't really exist — fabrication), `check_completeness` flags
`retrieved - claimed` (a real id the independent re-query found that the
claim omitted). Same generic shape, same caller contract (a fresh query +
a claimed id set) — existence grounding is not one mechanism, it's two,
same pattern as every other "X turned out to be (at least) two mechanisms"
finding already in this doc (scope-match, granularity precision).

Validated against CO-M2 live: golden is 24 obligations at
`confidence` 0.75/0.80 (3 + 21, both derived live, not hardcoded);
the actual recorded failing answer (15, missing 9 of the 0.80 band) is
reproduced live via the real confidence values, not invented. Locked in
with a test that pins down exactly why the fix was needed
(`test_existence_grounding_alone_misses_this_failure`: same failing claim
passes `check_existence` unmodified, fails `check_completeness`) —
`tests/test_stage4.py::TestCompletenessGrounding`, 4/4, plus a
non-regression case (an over-claimed-but-complete set must not be flagged
by completeness alone — that's still `check_existence`'s job). Composed
end-to-end in `tests/run_target_cases.py` (`CO-M2-failing`/`CO-M2-golden`)
and locked in by two `test_compose.py` checks. Added to Stage 3's
any-of grounding set (`pipeline/routing.py`) alongside the other three
grounding checks.

**What this does and doesn't close:**
- **CO-M2**: closed, but not held-out-validated — see the caveat in
  `fitness.py`'s module docstring. Any claim that this generalizes needs
  fresh data, same as everything else in this section establishes.
- **CO-M4** ("omitted the rubric-required non-overlap enumeration"): only
  *partially* addressable by the same mechanism, and not attempted this
  session — not silently glossed over. CO-M4's overlap claim itself was
  already complete (its failure wasn't a same-id-space omission like
  CO-M2's); what it dropped was an entire *second required deliverable*
  (a non-overlap enumeration) the question's rubric demands alongside the
  overlap mapping. `check_completeness` could in principle validate that
  second deliverable too, if applied to its own independently-derivable
  query (capabilities required by exactly one of the three regulations,
  not multiple) — but recognizing that a question requires *two* separate
  claimed-set checks in the first place is a Stage 2/3 multi-claim
  decomposition problem, not a Stage 4 mechanism gap, and decomposition
  execution is still out of v0 scope (see "Chosen build strategy" at the
  top of this doc). Don't force this one; it's a different, larger piece
  of work than CO-M2 was.
- **PM-H3**: untouched, and harder — it needs a genuinely new mechanism
  shape (counterfactual/what-if impact ranking), not a variant of
  existence or completeness grounding. Left as an open, honestly-scoped
  gap, not attempted.

## Overfitting-fix pass (same session — "what else should be fixed")

Direct follow-up to the audit above: asked what *other* overfitting issues
existed, beyond CO-M2. Found and fixed four more; found and deliberately
did not force a fifth. Same discipline throughout: verify live before
fixing, don't invent coverage the graph/skill doesn't actually support.

### Fix 1 — the vacuous-pass hole reopened (most severe finding)

Testing routing end-to-end against AU-H2 (a real target case, previously
only ever validated against Stage 1 in isolation — never actually
composed) found a live violation of "no false auto-pass": composed with
zero Stage 4 checks, AU-H2 produced `"Given the data currently in the
system, this is correct."` Root cause: `compose.py`'s mandatory-check
enforcement was keyed on `routing.mandatory_check_names` being non-empty.
AU-H2 is type D but not the hypothetical-chain shape, so `routing.py`
names no specific check for it — and the old condition treated "no check
named" as "no check required," silently falling through to
`FitnessResult.passed`'s vacuous `True` on zero checks. The exact same bug
class as AU-M4's original gap, just relocated: AU-M4 got a named
placeholder (`stale_chain_strict_reading`) because it was a specific case
reasoned about directly; every other B/C/D question landing in a
"no mechanism yet" routing branch had no such placeholder and fell
straight through.

**Fix:** enforcement is now keyed on `routing.path == DIRECT_MANDATORY_CHECK`
(a property of the type, per README: B/C/D's trigger check is "mandatory,
not optional"), not on whether a specific check happens to be named. When
`mandatory_check_names` is non-empty, one of those names must appear
(unchanged, stricter). When it's empty, *any* real Stage 4 check must have
run — weaker (doesn't confirm the check is the *relevant* one) but closes
the actual soundness hole. Verified this changes nothing for any existing
scenario (every B/C/D target case already carries a real check) and fixes
AU-H2 live. Locked in as a permanent regression scenario,
`AU-H2-zero-checks`, in `tests/run_target_cases.py` and
`tests/test_compose.py::test_au_h2_zero_checks_fails_closed_not_vacuously`.

**Why this matters beyond AU-H2 itself:** this fix also blunts the impact
of every remaining classification/regex imprecision below. Before it, a
wrong or missing routing signal could mean *zero* verification and a
confident pass. After it, the worst case for an unrecognized B/C/D shape
is "some real check ran, just not provably the right one" — a precision
gap, not a soundness one.

### Fix 2 — hypothetical-chain regex broadened and validated wider

`_is_hypothetical_chain` (built from AU-H4/SEC-H4's exact wording) was
already demonstrated, in the audit above, to miss paraphrases like
"should X stop working" and "assuming X is broken." Replaced the single
regex with two independent, both-required signals — a conditional marker
(`if`/`should`/`assuming`/`suppose`/`supposing`/`were to`) and a
failure/state verb (broadened list: fails, breaks, turns out, collapses,
malfunctions, stops working/functioning, goes down, ceases to
function/work). Still a keyword net, not NLP — documented as such, same
honesty as Stage 1/2's own disclosed limits. Validated against 6 must-match
cases (AU-H4, SEC-H4, plus the paraphrases that broke the old version) and
3 must-not-match cases (AU-H2, a real-time "is X currently failing"
question with no conditional marker, SEC-E1) in
`tests/test_routing.py::test_hypothetical_chain_detection_broadened_set`.

### Fixes 3+4 — question-type audit and verbatim-text audit (they turned out to be one finding)

Systematically diffed all 14 composed target cases' fixture text against
their verbatim source (`dev-questions.md` / `blind_questions.tsv`) and
independently re-derived each one's type (A–H) against README's own
per-type definitions, rather than trusting the assignments already made.

**Result: 12 of 14 match verbatim and re-derive to the same type already
assigned — confirmed, not just assumed.** Two didn't:

- **SEC-H1**: the fixture text (`"Which obligations require multi-factor
  authentication (MFA)?"`) was a paraphrase that silently dropped
  dev-questions.md's actual wording (`"If an attacker exploited a missing
  MFA check today, which regulatory duties across CRA/NIS2/GDPR would we
  be in breach of?"`) — and, with it, the hypothetical-chain framing that
  should have made this type D, not the type B it was assigned. The
  underlying golden answer (the same 7-obligation set) happens to be
  identical either way, which is why this wasn't caught by the mechanism
  failing — it was caught by re-deriving the type independently and
  finding it didn't match the real question's shape. **Fixed:** now uses
  the verbatim text, classified D. Confirmed live it correctly triggers
  the hypothetical-chain grounding requirement (needed adding
  `exploited`/`exploits` to fix 2's verb list — for this real question's
  own wording, not to force a synthetic case).
- **SEC-E1**: fixture text also paraphrased away part of the real
  question — dev-questions.md asks `"...and what state is each in?"`,
  which this pipeline's `check_existence` never verifies (it only checks
  which Control *ids* are present, not their `implementation_status`).
  Type B is still correct either way, so no reclassification — but this
  is a real, undiscussed completeness gap in SEC-E1's own validation,
  structurally the same shape as CO-M4's "second required deliverable"
  gap. **Not fixed this session** — extending `check_existence`/
  `check_completeness` to verify a second attribute per id (not just
  membership) is a real signature change, not a quick patch, and doesn't
  have its own validated target case yet. Flagged here so it isn't lost.

### Fix 5 (audit only — correctly did not force a fix)

Checked whether there's a *grounded* basis (not just "add more keywords")
to expand Stage 1's alias table or Stage 2's structural patterns.

- **Stage 1**: re-read `ps-domain/SKILL.md`'s Canonical Definitions
  section in full. It defines exactly two boundary terms — Overdue and
  Stale (deprecated is referenced within Overdue's own definition, not a
  separate one) — and the alias table already covers both. **There is
  nothing else to add without inventing an undefined term**, which would
  be exactly the failure mode Stage 1 exists to avoid, not fix. Stage 1's
  narrowness isn't an arbitrary gap; it's complete relative to what's
  actually defined today.
- **Stage 2**: individually examined all 5 of the documented 2/7
  spot-check misses (AU-M2, EM-H2, SA-H2, PM-H1, RM-H2) to see if a
  principled (not reverse-engineered-from-these-5) pattern exists.
  Three don't: EM-H2's failure ("Give me a one-paragraph summary...") is
  invisible in the question text by construction — no count language
  exists to detect, confirming PROGRESS.md's own prior claim that this
  class is caught downstream, not by Stage 2. AU-M2 and SA-H2 are the
  same shape (the failure-kind label and the text-pattern space just
  don't line up 1:1 for these). PM-H1 has no clean pattern without
  reverse-engineering from a single example. **RM-H2 is the one real
  opportunity**: it's comparison-shaped ("benchmark X against Y"), and
  `pipeline/stage5_sampling.py`'s `_is_comparison_shaped` heuristic
  already exists for a different purpose (Stage 5's dry-run sampling) and
  was already flagged there as "do not promote to `pipeline/` proper
  without the same must-flag/must-not-flag validation discipline the real
  mechanisms got." That promotion is real, well-scoped follow-up work —
  not attempted this session, so as not to rush an unvalidated heuristic
  into a live signal path the way the original hypothetical-chain regex
  was.

### Net effect

74 tests pass (was 72 before this pass — +1 hypothetical-chain-detection
test, +1 AU-H2 regression test). Zero regressions across every existing
scenario. The severity ordering going in (vacuous-pass hole > regex
precision > classification/text audit > alias-table completeness) held up
against actual investigation — the first finding was the only one that
touched the "no false auto-pass" invariant directly; everything else was
precision, not soundness.

## Precision tests on previously-untouched held-out passes (same session)

Follow-up to a direct question: would running the rest of `blind_
questions.tsv`'s 54 questions through the pipeline be meaningful? Answer
worked out in two parts, not just asserted:

- **For recall** (does it catch new failures): no value left. All 10 of
  the held-out set's known failures are now accounted for — 7 were used
  to design mechanisms, the other 3 (CO-M2, CO-M4, PM-H3) were just
  audited above. The remaining 44 are passing instances by definition, so
  they can't test recall at all.
- **For precision** (does it avoid false-flagging good answers): most of
  the 44 touch no existing mechanism's domain at all (breach-notification
  timing, effective dates, refusal cases, open-ended summaries) —
  composing those would only ever produce "no mechanism," which isn't new
  information. Checked all 44 against the 7 mechanisms' actual domains and
  found exactly 3 clean candidates: passing questions that reuse an entity
  an existing mechanism already covers, never composed before.

**Result: all 3 confirmed correct, live.**

| ID | Question | Mechanism reused | Result |
|---|---|---|---|
| SA-E3 | "Do NIS2 or GDPR need our SBOM capability for anything today?" | `check_regulation_scope`, same capability as SA-H1 | Golden claim (CRA-1.0 only) not flagged |
| SA-M2 | "What capabilities does our internal Helvex SOP have in common with the CRA?" | `check_existence`, a **freshly constructed** intersection query (CRA's required-capability set ∩ HELVEX-SOP's) — not SA-H1/AU-H4's query reused verbatim | Intersection independently confirmed to be exactly `{cap_security_logging_c4d9e2}`, matching RUNBOOK; golden claim not flagged |
| PM-M3 | "GDPR requires records of processing and DPIAs — do our policies actually cover both duties?" | `check_evidence_gap_root_cause`, twice — DPIA reuses EM-M4's capability (already "governance"); Art 30/ROPA is `cap_compliance_documentation_management_a87281`, a capability **no prior mechanism had touched**, independently classified "governance" live | Both duties confirmed governance gaps, matching RUNBOOK's "DPIA draft-only + Art. 30 ungoverned"; golden claim not flagged |

**One real gap found and fixed along the way, not just a confirmation:**
building PM-M3 exposed that `evidence_gap_root_cause` had never been added
to Stage 3's any-of grounding-check set (`routing.py`) — an omission from
when the type-C-specific root-cause branch was built for EM-M4, before
this precision test needed the same mechanism reachable from type B's
generic "no disambiguation" branch too. Added; existing `test_routing.py`
assertions updated to match.

Composed end-to-end as `SA-E3-golden`, `SA-M2-golden`, `PM-M3-golden` in
`tests/run_target_cases.py` (golden-only — these are correct transcripts,
not recorded failures, so there's no failing variant to reproduce). 27
scenarios total now (was 24), all three-block-complete, gate-failed count
unchanged at 12 (all three pass cleanly, as expected). No new test
methods needed — `test_compose.py`'s generic loops
(`test_every_scenario_is_three_block_complete`,
`test_golden_scenarios_get_a_confident_statement`) already cover new
scenarios automatically.

**What this does and doesn't add to the overfitting picture:** three more
data points that the mechanisms don't false-flag correct answers on
material they were never built from — real, but modest, since two of the
three reuse capabilities already validated (SA-E3 fully, PM-M3's DPIA
half). The one genuinely new entity (PM-M3's ROPA capability) held up.
This doesn't change the recall picture at all — CO-M4 and PM-H3 are still
open, and a fresh held-out set is still the only path to new recall
evidence, exactly as concluded in the audit above.

---

## Open questions (not blocking, but don't lose track)

- ~~Pharma-auditor acceptance bar is unvalidated.~~ — done this session
  (see "Pharma-auditor-acceptance-bar manual pass" above). Gate discipline
  passes; `source_ref`-to-regulation-text rendering in (C) is the concrete
  gap standing between "mechanically verified" and "auditor-signable" —
  tracked as the top item in "Next action" above, not re-opened here.
- ~~EM-M4's entity-type mismatch is weaker/fuzzier than EM-E3's — may need
  a different mechanism than a clean entity-type cross-check, or may just
  be a partial-credit case. Don't force a fit; note honestly if it
  doesn't cleanly validate.~~ — resolved a later session: it needed a
  different mechanism, not partial credit. The dimension is root-cause
  (governance vs. engineering), not counting-unit — `check_evidence_gap_
  root_cause`, validated live against RUNBOOK's own "10 and 10" note. See
  "Stage 4 — root-cause classification" above.
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
