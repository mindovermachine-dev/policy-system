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

## Build status

| Component | Status |
|---|---|
| README.md | done (rev. 2026-08-11: falsification step, rubric measured-vs-assumed note, step-2 freehand clarification) |
| PROGRESS.md | this file |
| Static rubric doc | done ([RUBRIC.md](./RUBRIC.md), v2 2026-08-11: criterion IDs, tiered scoring, gate logic, guardrails) |
| Socratic authoring skill / stand-in | not started |
| Freehand-retrieval CLI surface | not started |
| Fitness-loop harness | not started |
| Falsification loop | not started |
| Answer-construction variance experiment (candidate) | not started |
| Run vs. CO-M4 | not started |
| Run vs. PM-H3 | not started |

## Next action

Build the Socratic question+fitness-authoring skill as a spike-local skill
(Setup step 2), authored against [RUBRIC.md](./RUBRIC.md).
