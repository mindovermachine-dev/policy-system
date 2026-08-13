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

## Next action

Review `smoke-test/run-02-harder-pilot.md`'s findings, specifically:

1. D5 is now resolved: the 0/45-across-both-rounds clean streak on real
   questions is well-supported as a fact about this graph's current data
   cleanliness (confirmed directly — no `SUPERSEDED_BY` edges, no
   deprecated data anywhere), not a gap in falsification effort — the
   seeded-defect set's 3/3 first-attempt landing rate under blind
   isolation is the load-bearing evidence for that conclusion.
2. NP-002's "shared (2+)" vs. "a few" framing gap and NP-005's
   keyword-sensitivity finding (D4) are still open candidates for
   hardening into a fixed falsification angle, per README.md step 6 —
   still out of scope for this spike, flagging for whoever picks it up
   next. NHQ-4's "does the ranking survive a third independent proxy"
   check (D5) is a similar candidate, one data point so far.
3. The known FalkorDB `count(*)`/multi-`DISTINCT` under-reporting defect
   (`pipeline2/run-02` Q4) did not reproduce despite NHQ-2 deliberately
   courting its shape twice — worth a note for whoever next investigates
   that defect; not proof it's fixed or gone, just not triggered by the
   shapes tried here.
4. No other infrastructure/dialect issues surfaced this round — nothing
   new to add to the Environment section above.
5. D6's scope-aware attempt cap is a design change made *from* the pilot
   findings, not *validated by* a pilot itself — neither run-01 nor run-02
   touched `Policy`/`Standard`/`Control`, so the 5-attempt cap on that
   layer and the 1-attempt floor's safety outside it are both currently
   reasoned from the domain model's Lifecycle text, not measured. A third
   pilot round with questions that actually route through
   `Policy`/`Standard`/`Control` would be the natural next empirical check,
   if this spike continues.
