<!-- © 2026 Cartman ApS. All rights reserved. -->
# Progress Tracker — Guided Fitness Pipeline

Resumable build state for [README.md](./README.md)'s design.

## Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Fitness functions matched 1:1 to their question, not reused across questions | **Resolved** — no single check shape generalized in `compliance-decision-pipeline`, confirmed empirically (scope-match, granularity each split into ≥2 mechanisms) |
| D2 | Fitness evaluation must be deterministic (query + predicate), not LLM judgment of prose | **Resolved** — every viable Stage 4 check in `compliance-decision-pipeline` was already this shape; keeps the loop from grading its own homework |
| D3 | User approval scope: English question only, or also the literal generated query/predicate | **Resolved** — both; approving English alone can hide a mistranslated query |
| D4 | Static rubric governs authoring, not per-question fitness reuse | **Resolved** — rubric content drafted in README, needs its own standalone doc before Setup step 2 |
| D5 | Relationship to `compliance-decision-pipeline` / `e2e-pipeline` | **Resolved** — both stay frozen/independent; no shared code ([[spike-independence-no-shared-code]]) |
| D6 | Loop budgets: `max_turns` for the refine loop AND `max_falsification_attempts` for falsification, and best-effort output shape when either is hit | **Resolved** — both budgets are CLI-configurable parameters. `max_turns` has no tuned default; it's set empirically from run-log data, with a generous safety-cap value in the meantime purely as a hang-guard (not a claimed-correct default). `max_falsification_attempts` keeps README's default of 5. Verification best-effort output = last candidate answer + full failure history (every prior attempt's fitness failure, not just the last). Falsification best-effort output = README's existing narrative ("answer survived N attempts," landed/missed per attempt) — already sufficient, no additional schema needed |
| D7 | Freehand retrieval surface: new thin CLI vs. reuse of `ps.py`'s query pattern (not its code) | **Superseded by D10** — connection pattern + read-only guard reused; query routing is not |
| D8 | Falsification step: fixed taxonomy vs. freehand LLM-authored | **Resolved for the spike** — freehand, capped per D6; spike tests LLM falsification creativity before hardening any angle into the rubric. Revisit after run-log data |
| D9 | Step 1 actor split: who authors vs. who validates the fitness function | **Resolved** — skill authors, harness validates against the static rubric |
| D10 | Step 2 retrieval: CLI template routing vs. freehand Cypher | **Resolved for the spike** — genuinely freehand; ps-domain schema rules apply as skill context, its command-preference routing does not. Revisit after run-log data |
| D11 | Fitness-function rubric scoring methodology: adopt structure from `inspiration/business-challenge-rubric.md` + `inspiration/bc-scoring-methodology.md`, or keep the flat 5-bullet valid/invalid check | **Resolved** — adopted: per-criterion IDs, Pass(2)/Partial(1)/Fail(0) tiers, explicit pass-gate expression, scoring guardrails (RUBRIC.md v2). Declined for this revision: evidence-citation requirement, multi-perspective scoring. This governs how the harness scores a fitness function's own text against RUBRIC.md's 5 criteria (step 1) — does not touch D2, which governs the fitness function's own execution against the candidate answer (step 4) |
| D12 | Step 1 actor split, revisited: D9 said one skill authors both the narrowed question and the fitness function. Split further into two skills, or keep as one? | **Resolved — superseded D9's single-skill framing.** Split into two skills: `.github/skills/policy-question/SKILL.md` (Socratic narrowing only, grounded in `docs/artifacts/ps-domain-concepts.md`, outputs refined question text + a short labeled list of entities/edges it routes through — no fitness function, no retrieval, no answer) and a separate, not-yet-built fitness-authoring skill consuming that output. Placement also revisited: built under `.github/skills/` (matching the repo's only existing skill-packaging convention — `ps-domain`, `reasoning`) rather than spike-local as README originally said, since no prior spike had actually established a spike-local skill pattern to follow |
| D13 | Whether freehand retrieval (step 2) and answer construction (step 3) get added directly into `policy-question` ahead of the fitness-authoring skill/harness, or wait for the full harness build | **Resolved** — added directly to `policy-question` (rev. 2026-08-12, v0.2.0), per user request to validate each pipeline addition's value incrementally (consistent with the smoke-test discipline in `smoke-test/LEARNINGS.md`) rather than building the full harness at once. Retrieval reuses `spikes/e2e-pipeline/ps.py cypher` directly (as the smoke tests already did) — no new CLI surface built. Step 1b (fitness-function authoring), step 4 (verification loop), and step 5 (falsification) remain deliberately **not** added; the skill's own output now carries an explicit "unverified" status line so this partial state can't be mistaken for a checked answer. Does not reverse D12 — narrowing and fitness-authoring stay two separate skills; this front-loads steps 2-3 into the narrowing skill ahead of the harness originally slated to own them |
| D14 | Whether to test Socratic narrowing (steps 1-4) before building further, and how | **Resolved** — piloted via simulated naive-user dialogue (per `smoke-test/LEARNINGS.md`'s open item), using 5 questions selected from `docs/test-data/dev-questions.md`'s non-Helvex set (see `docs/test-data/narrowing-pilot-questions.md`, NP-001..005) rather than freshly invented ones. Each ran as a single self-play subagent (skill-follower + simulated persona-user in one process, with an explicit anti-leakage discipline) — weaker than `run-02`'s true cross-agent isolation, flagged as a known limitation, not yet upgraded. All 5 converged in 4-5 questions; no confirmed vocabulary leakage. Found 4 real gaps in the skill's own text, now fixed in `policy-question` v0.2.1: no handling for compound multi-part questions (NP-003), no handling for questions with no existing graph anchor/hypothetical systems (NP-005 — most severe, produced a degenerate no-edge output), no handling for comparisons across a deliberately non-convergent layer like Role (NP-001), and silent unprompted handling of an entity with no status property (NP-002, Obligation). Same shape of value as run-01/02/03: a cheap test surfacing real, specific gaps before further build-out |
| D15 | Whether v0.2.1's fixes actually changed behavior, verified by rerunning the same 5-question pilot | **Resolved** — reran NP-001..005 against v0.2.1 with the identical isolation protocol. All 4 targeted gaps fixed as intended: NP-001 and NP-003's fixes turned a previously-silent judgment call into a real, confirmed Socratic question; NP-002's fix surfaced the missing-status problem explicitly instead of improvising; NP-005 went from a degenerate no-edge output to a real traversal with an explicit, user-confirmed theme-filter caveat — better than the minimum fix required. NP-004 (control question, unaffected by the 4 gaps) showed no regression — new checklist items substituted for genuinely relevant questions rather than adding rote friction. Two follow-on refinements found by the rerun's own self-assessments, now fixed in v0.2.2: (1) the Output shape's Edges line was binary (edges, or "none") and had no way to state "real traversal + an unmodeled filter term layered on top," the exact shape NP-005 produced; (2) the status/lifecycle checklist item fixed Obligation's missing-status case but didn't address the case where *multiple* upstream entities in a chain carry independent status (Regulation.status vs. Requirement.status can diverge) — NP-002's rerun silently picked one. Caveat carried forward: every comparison is n=1 per condition, still self-play not cross-agent isolation — read as suggestive, not proven, per this spike's own no-verdict-inflation discipline |
| D16 | What the full pipeline (steps 1-7, real retrieval against the live graph) surfaces beyond what the narrowing-only pilot could | **Resolved** — reran NP-001..005 through the complete skill (v0.2.2), real Cypher against `policy_system`. Found 5 issues invisible to narrowing-only testing, now fixed in v0.3.0: (1) the v0.2.2 status-disambiguation fix didn't reliably apply — NP-002's run asked it, NP-004's run (same skill version) silently skipped it, so the checklist item is now a mandatory pre-flight gate at step 3 rather than a discretionary loop question; (2) no guidance existed for retrieval-time entity-instance ambiguity (NP-003 found 2 real candidate Capability nodes matching one fuzzy name; got lucky that only one connected to data) — step 5 now requires reporting multiple connected candidates separately rather than silently picking one, since the approval gate has already passed by then; (3) the no-graph-anchor checklist item let NP-005 default straight to a bare keyword filter without checking whether the classification layer (PracticeArea/RiskPath) had a real matching category first (it did: `RiskPath.risk_type: privacy`) — now checked first; (4) step 5's phrasing implied one query, but every run this time needed 2+ (discovery queries, aggregate cross-checks) — now explicitly blessed; (5) attribute/theme-filter answers carry a distinct uncertainty (sensitivity to keyword choice) the generic "unverified" Status line didn't name — now stated separately when that mode is used. Not fixed (different artifact, smaller ask, not yet actioned): `ps-domain-concepts.md`'s Worked Examples only show CRA's `Regulation.id` format, so NIS2/GDPR's had to be guessed by every retrieval run — worked every time so far, but undocumented |
| D17 | Whether a fitness function can be usefully hand-authored against RUBRIC.md v2 alone — no fitness-authoring skill built yet — and actually verify a real candidate answer when executed | **Resolved** — yes, per `smoke-test/run-04-hand-authored-fitness.md`. Hand-authored fitness functions for both `fixtures.md` fixtures (NQ-001, single-edge; NQ-002, six-edge chain), each live-executed against `policy_system` alongside the real candidate answer. NQ-001's first draft failed its own gate (FF-REQ-001 honestly Partial — only one schema path exists to the claim) and its gate-legal query had a latent defect only live execution caught (predicate 1 wasn't derivable from the query's own output); fixed and reconfirmed. NQ-002 passed cleanly first try, including a genuine FF-REQ-001 Pass via a structurally distinct derivation. **New structural finding:** independent-re-query quality (FF-REQ-001) is gated by claim complexity — single-hop factual claims should be expected to cap at Partial, not chased toward Pass; multi-hop claims have real room to diverge. Also surfaced: FalkorDB rejects `EXISTS {}` subqueries and filtered-alias pattern-predicates (undocumented constraint on freehand retrieval, see Environment below), and a domain-data finding (100% of active-CRA Requirements currently lack a complete implementation chain in this test graph — verified not a bug). Does not resolve D9/D12's actor split — a real fitness-authoring skill is still not built; this only proves the manual path is viable evidence to build that skill against |

## Environment

- FalkorDB at `localhost:6379`, graph `policy_system`, via `/usr/bin/python3`
  (same as every prior spike)
- Held-out discipline: CO-M2/CO-M4/PM-H3 are `compliance-decision-pipeline`'s
  spent-vs-unspent set. CO-M2 was used as design reference there (spent).
  **CO-M4 and PM-H3 are still genuinely unseen by any mechanism** — reserved
  as this spike's generalization test. Do not look at them for design
  inspiration before the rubric and fitness functions are authored.
- Run-log schema must record **which pipeline step produced each failure**
  (retrieval data vs. answer construction vs. falsification). Freehand
  retrieval (D10) failing would otherwise contaminate the read on
  falsification creativity (D8) — attribution is a schema requirement, not
  a nice-to-have.
- FalkorDB's Cypher dialect rejects `EXISTS { MATCH ... }` subquery blocks
  and pattern-predicates with inline property maps referencing
  externally-bound variables (`Unable to resolve filtered alias`) — both
  legal in mainstream/Neo4j-style Cypher. Found in D17/run-04. Anyone
  freehand-authoring Cypher against this graph should expect these to
  fail and use a plain `MATCH ... WHERE prop = $var` instead.

## Build status

| Component | Status |
|---|---|
| README.md | done (rev. 2026-08-11: falsification step, rubric measured-vs-assumed note, step-2 freehand clarification) |
| PROGRESS.md | this file |
| Static rubric doc | done ([RUBRIC.md](./RUBRIC.md), v2 2026-08-11: criterion IDs, tiered scoring, gate logic, guardrails) |
| Socratic narrowing skill | done (`.github/skills/policy-question/SKILL.md`, rev. 2026-08-11, per D12) |
| Freehand retrieval + answer construction (steps 2-3) | done, folded into `policy-question` (rev. 2026-08-12, v0.2.0, per D13) — reuses `spikes/e2e-pipeline/ps.py cypher` directly, no new CLI surface |
| Fitness-authoring skill | not started — but hand-authoring proof-of-concept done (D17, `smoke-test/run-04-hand-authored-fitness.md`), evidence the skill is worth building |
| Fitness-loop harness | not started |
| Falsification loop | not started |
| Answer-construction variance experiment (candidate) | not started |
| Run vs. CO-M4 | not started |
| Run vs. PM-H3 | not started |

## Next action

D17's hand-authoring proof-of-concept (run-04) is done and positive —
build the fitness-authoring skill (Setup step 2, second half), authored
against [RUBRIC.md](./RUBRIC.md), taking the skill's refined question as
its input. Carry run-04's structural finding into the skill's own
guidance: don't tune it to chase an FF-REQ-001 Pass on single-hop factual
claims, since that's a structural ceiling, not an authoring gap. Separately,
running `policy-question` v0.3.0 against a few more questions (per D13's
incremental-validation discipline) remains open but is no longer blocking.
