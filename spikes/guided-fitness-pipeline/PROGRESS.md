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
| D6 | `max_turns` value for the refine loop, and best-effort output shape when hit | **Open** |
| D7 | Freehand retrieval surface: new thin CLI vs. reuse of `ps.py`'s query pattern (not its code) | **Open** |

## Environment

- FalkorDB at `localhost:6379`, graph `policy_system`, via `/usr/bin/python3`
  (same as every prior spike)
- Held-out discipline: CO-M2/CO-M4/PM-H3 are `compliance-decision-pipeline`'s
  spent-vs-unspent set. CO-M2 was used as design reference there (spent).
  **CO-M4 and PM-H3 are still genuinely unseen by any mechanism** — reserved
  as this spike's generalization test. Do not look at them for design
  inspiration before the rubric and fitness functions are authored.

## Build status

| Component | Status |
|---|---|
| README.md | done |
| PROGRESS.md | this file |
| Static rubric doc | not started |
| Socratic authoring skill / stand-in | not started |
| Freehand-retrieval CLI surface | not started |
| Fitness-loop harness | not started |
| Run vs. CO-M4 | not started |
| Run vs. PM-H3 | not started |

## Next action

Resolve D6/D7, then write the static rubric as its own doc (README's draft
is a starting point, not final) before building anything that depends on it.
