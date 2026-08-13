<!-- © 2026 Cartman ApS. All rights reserved. -->
# Progress Tracker — Guided Fitness Pipeline pass 3

Resumable build state for [README.md](./README.md)'s design.

## Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Whether completing this spike requires an empirical pilot run (falsification executed against real questions/answers, results logged) before it counts as done, or landing the step's text is sufficient | **Resolved** — pilot run required, against multiple questions (not just one). Directly addresses the "confirmation theater" failure mode this spike's own README names, which can only be checked empirically |
| D2 | Where the falsification step lives: folded directly into `tools/skills/policy-question.md` (matching pipeline2 D13's precedent), or a separate file | **Resolved** — separate file, `tools/skills/falsification-step.md`, invoked by `policy-question.md` after answer construction. Not packaged as a skill (no on-load phase, no independent entry point) — same non-skill-instruction pattern as `tools/skills/reasoning.md`, kept in the same directory rather than a new `tools/instructions/` for consistency with that existing precedent |
| D3 | Whether pipeline3 gets its own decision log like pipeline2's PROGRESS.md, or records decisions inline in README.md's Setup section | **Resolved** — yes, this file |
| D4 | What a pilot run of the full pipeline (narrowing → retrieval → answer → falsification) against multiple real questions actually finds | **Resolved** — see `smoke-test/run-01-falsification-pilot.md`, NP-001..005 (reused from `spikes/pipeline2/narrowing-pilot-questions.md`), self-play (skill-follower + simulated persona-user in one process, per pipeline2 D14's known-weaker-than-cross-agent-isolation caveat — same limitation applies here), falsification run in the *same* context as answer construction per this spike's actual shipped design (not `run-02-subagents.md`'s stronger isolated-falsifier test, which validates a different mechanism than what's being built). **Falsification landing rate: 0/20 attempts landed** a literal contradiction across all 5 questions — compare `pipeline2/smoke-test/run-02-subagents.md`'s 1/17, where the one landing needed cross-source node-identity reasoning (two Regulation nodes = one real source at different versions) that this graph's CRA/NIS2/GDPR/ENGPRAC data doesn't currently present (checked directly via NP-001/NP-003's identity checks — no shared Obligations across regulations were found, unlike run-02's Helvex pair). **No confirmation-theater signal**: all 20 attempts used mechanically distinct angles (aggregation cross-verification, duplicate-node/edge detection, active-filter bypass, phrase-variant and keyword-breadth sweeps, cross-layer/sibling-RiskPath checks, content-plausibility sampling, denominator sizing) — no repeated weak angle in different words. Two non-landing attempts materially improved the delivered answer regardless: NP-004's broadened sweep caught GDPR's Art. 30 records-of-processing duties as a real but out-of-scope-by-name adjacent finding; NP-005's broadened keyword sweep roughly doubled the genuinely relevant Capability count (8→15 GDPR-confirmed) and caught 3 concrete keyword false positives a narrower pass would have shipped uncaught. **No answer defect invalidated a headline claim**, though NP-002's approved "2+" shared-vs-single-use threshold was shown (via a top-N recompute, not a landed disproof) to answer a technically different question than the persona's own "a few" framing — worth surfacing to whoever reviews the answer, not something the falsifier alone can fix. No FalkorDB dialect gaps (`EXISTS {}`, filtered-alias predicates) or the known multi-`DISTINCT` `count(*)` under-reporting bug were triggered — every aggregate here used at most one `DISTINCT` column per `count()`, and every place a cross-check was run to be safe (NP-002, NP-003) agreed exactly. Caveat carried forward: self-play, n=1 per question, read as suggestive not proven, per this spike's own no-verdict-inflation discipline |
| D5 | Whether the 0/20 clean streak (D4) reflects this graph's data shape or the falsifier's own creativity ceiling, and how to test that distinction rather than just running more of the same kind of question | **Resolved** — see `smoke-test/run-02-harder-pilot.md`. Two-part design, deliberately different isolation levels: (1) NHQ-1..5, five fresh natural-hard questions targeting structural traps actually present in this graph (fan-out concentration, the known FalkorDB multi-`DISTINCT` aggregation bug from `pipeline2/run-02` Q4 deliberately courted twice — did not reproduce, a genuine negative result — 4-way regulation convergence, proxy-metric ranking ambiguity, a 3-clause compound question), self-play, same D14 caveat as D4; (2) SEED-1..3, three real Q&A pairs (genuine retrieval, genuine data) each given one deliberate construction-step defect — a numeric misstatement, an incomplete-retrieval overclaim, and a raw-row/distinct-count confusion — with ground truth logged privately before any falsifier saw the materials, each handed to a **separate isolated Agent-tool subagent** (no shared context, no hint the batch contained defects) with exactly `falsification-step.md`'s stated preconditions. **Result: natural-hard 0/25 landed (25 more genuinely varied attempts, two of which materially strengthened answers without landing — same pattern as D4); seeded-defect 3/3 landed, each on the first attempt**, including SEED-2 and SEED-3 where the defect was not visible in the data handed to the falsifier and required it to query or reconcile independently. This directly answers D5's question: across both rounds, 0/45 on real questions is now well-supported as evidence about this graph's current data cleanliness, not about the falsifier failing to try — when a genuine, checkable contradiction exists, blind isolated falsification lands it immediately. Also confirmed directly (not assumed): the `SUPERSEDED_BY`/cross-source-identity trap that made `pipeline2/run-02`'s hard tier land does not exist anywhere in this graph (no `SUPERSEDED_BY` edges, no deprecated Requirement/Capability/PracticeArea/RiskPath at all) — reproducing that specific landing mechanism would require loading data that actually contains a duplicate-source pair, not just asking harder questions of the current graph |
| D6 | Whether every question should pay for the full falsification cost, given D5 showed real value exists but is cheap to catch (every landing/material-improvement across both pilots happened on attempt 1) and the domain model itself distinguishes ingested, read-only-once-created data (`Regulation`, `Requirement`) from actively customer-governed data (`Policy`: "created by policy managers through governance workflows... revised when regulations or the business change") | **Resolved** — scope-aware attempt cap, not a global on/off toggle (an earlier "off by default, opt in" framing was rejected as backwards: the customer-governed layer is exactly where a defect is most plausible, so defaulting off there means silence in the highest-risk case). Falsification always runs at least once, regardless of scope — never skipped. `max_falsification_attempts` is **1** if the question's approved Entities stay within the ingested spine (`Regulation`/`Role`/`Requirement`/`Obligation`/`Capability`/`PracticeArea`/`RiskPath`), or **5** if `Policy`/`Standard`/`Control` are involved, or the user explicitly asks for deeper scrutiny. The 1-attempt floor is evidence-backed for the layer both pilots actually tested (D4/D5's combined 0/45 + 3/3-on-attempt-1 result); it is explicitly **not** empirically tested against Policy/Standard/Control, which is the actual reason that layer keeps the full cap rather than inheriting the floor — not yet evidence the higher cap is needed there either, just that it hasn't been checked. Implemented in `tools/skills/falsification-step.md` v0.2.0 (new "Determine the attempt cap" section ahead of Process) and wired into `tools/skills/policy-question.md` step 7 and its Guardrails, and `README.md`'s Core loop description |

| D7 | Whether the third pilot round (D6's flagged next step: test the untested Policy/Standard/Control 5-attempt cap) should be re-scoped after `9baacf4` ("connected engineering practices with eu regulations in the graph") merged 6 of ENGPRAC's 12 Capability nodes onto the existing CRA/GDPR/NIS2 canonical Capability nodes they duplicated, instead of leaving them as separate ENGPRAC-native nodes | **Resolved** — combine, not separate rounds (user's explicit choice). Confirmed live in the graph (queried directly, not assumed from the diff): `cap_secure_development_lifecycle_9f3224` now has 7 inbound `REQUIRES` edges spanning CRA-, NIS2-, and ENGPRAC-sourced Obligations. Full fan-in map queried: `cap_access_control_authentication_151816` converges GDPR+NIS2+CRA+ENGPRAC (4-way); `cap_secure_development_lifecycle_9f3224` and `cap_vulnerability_management_55d0c4` each converge 3 sources; `cap_component_inventory_sbom_management_b5223c`, `cap_data_protection_by_design_default_69e489`, `cap_security_logging_c4d9e2` each converge 2. `cap_security_logging_c4d9e2` converging CRA+ENGPRAC is the exact node `ps-domain-concepts.md`'s own Worked Example 2 / Convergence section names as the model's canonical illustration — the seed data just started actually instantiating what the model was always designed to produce, not a novel structure. 4 Capabilities remain ENGPRAC-native by design (no regulation-side counterpart, confirmed in `capability_merges.json`'s "KEEP NATIVE" notes): `cap_policy_exception_governance_3cfd12`, `cap_quality_gate_assurance_10ab6e`, `cap_release_change_control_2ec781`, `cap_service_reliability_management_a3c0fe`. This partially stales D5's "no cross-source-identity trap exists in this graph" finding: that finding was specifically about regulation-version identity (`SUPERSEDED_BY`, still genuinely absent), not about Obligation fan-in at a shared Capability across an ingested regulation and the customer-governed layer — which is now live and untested. Rationale for combining rather than running a separate round: all 6 merged Capabilities `GOVERNED_BY` a Policy, so a question that walks a converged Capability into Policy/Standard/Control naturally exercises both the still-untested 5-attempt cap (D6) and the new convergence-fan-in structure in one pass — accepted tradeoff (per the question asked before this decision) is that a landed disproof in a combined question may need extra judgment to attribute to one mechanism or the other, rather than cleanly isolating which one is at fault. Scope: `smoke-test/run-03-convergence-pilot.md`, same two-part structure as D5 (natural-hard self-play + seeded-defect isolated-subagent set) |

## Environment

Same as pipeline2 (see `spikes/pipeline2/PROGRESS.md`'s Environment
section): FalkorDB at `localhost:6379`, graph `policy_system`, via
`/usr/bin/python3`; FalkorDB's Cypher dialect rejects `EXISTS { MATCH ... }`
subqueries and filtered-alias pattern-predicates — falsification queries
should expect this and use plain `MATCH ... WHERE prop = $var` instead.

## Build status

| Component | Status |
|---|---|
| README.md | Setup section filled in |
| PROGRESS.md | this file |
| `tools/skills/falsification-step.md` | done (v0.2.0 — scope-aware attempt cap, see D6) |
| `tools/skills/policy-question.md` wiring (Process step 7, Output template, Purpose/Deliverable, Guardrails) | done, updated for D6's scope-aware cap |
| Pilot run (multiple questions, live graph) | done — `smoke-test/run-01-falsification-pilot.md` (NP-001..005, 0/20 falsification attempts landed, no confirmation-theater signal) |
| Round 2 pilot (harder natural questions + seeded-defect isolated falsifiers) | done — `smoke-test/run-02-harder-pilot.md` (NHQ-1..5: 0/25 landed; SEED-1..3: 3/3 landed under blind isolation — see D5) |
| Scope-aware attempt cap (D6) | done — `falsification-step.md` v0.2.0, wired into `policy-question.md` and `README.md`'s Core loop |
| Round 3 pilot (EU-regulation/ENGPRAC convergence + first real exercise of the 5-attempt cap) | done — `smoke-test/run-03-convergence-pilot.md` (NHQ3-1..4: 0/12 landed, including one question walking a converged Capability through Policy/Standard/Control; SEED3-1..3: 3/3 landed under blind isolation — see D7) |

## Next action

Review `smoke-test/run-03-convergence-pilot.md`'s findings, specifically:

1. D7 is now resolved with data: the 9baacf4 merge's convergence structure
   (6 Capabilities now fan-in from both EU-regulation and ENGPRAC-sourced
   Obligations) was directly targeted by 4 natural-hard questions and 3
   seeded-defect pairs. 0/12 landed on the real questions; 3/3 seeded
   defects landed on attempt 1 under blind isolation — the same
   clean-data-not-weak-falsifier signature D4/D5 established, now
   confirmed on this specific new structural shape rather than assumed to
   generalize from it.
2. D6's 5-attempt cap on Policy/Standard/Control got its first real
   exercise (NHQ3-3, walking the Security Logging convergence through to
   Control level) and ran its full 5 attempts cleanly, no false positive.
   This is one question, not a systematic sweep of the
   Policy/Standard/Control layer on its own terms (e.g. draft-status
   Policies, deprecated Standards, or `Control`/`Standard` lifecycle edge
   cases independent of Capability convergence) — still a thinner sample
   than the ingested-spine layer's now three rounds of testing.
3. Two non-landing attempts (NHQ3-3 attempt 3, NHQ3-4 attempts 3 and 5)
   found that RiskPath and Role are deliberately cross-cutting across the
   native/shared Capability boundary, while Policy and PracticeArea are
   not — a scoping nuance an unscoped answer could overclaim. Candidate
   for the same "flag, don't harden yet" treatment as NP-002/NP-005/NHQ-4
   below, per README.md step 6.
4. Carried forward, still open, still out of scope for this spike: NP-002's
   "shared (2+)" vs. "a few" framing gap, NP-005's keyword-sensitivity
   finding (D4), and NHQ-4's "does the ranking survive a third independent
   proxy" check (D5) — all still candidates for hardening into a fixed
   falsification angle, not yet acted on.
5. The known FalkorDB `count(*)`/multi-`DISTINCT` under-reporting defect
   (`pipeline2/run-02` Q4) did not reproduce again this round either
   (NHQ3-2's distinct-regulation/distinct-obligation split matched a
   direct recount exactly) — third round in a row it hasn't triggered,
   still not proof it's fixed.
6. No other infrastructure/dialect issues surfaced this round — nothing
   new to add to the Environment section above.
