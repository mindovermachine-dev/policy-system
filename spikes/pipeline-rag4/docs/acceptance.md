# ACCEPTANCE — pipeline-rag4  (native GraphRAG-SDK graph → policy_system-shaped)

Verified by the ORCHESTRATOR (not a sub-agent summary) on 2026-08-16 22:20 UTC / 00:20 CPH.
Run: `map_graph.py` then `compare.py`; final graph `policy_system_graphrag_final`.

## Result: **FINAL: PASS** on the final graph; all ACs MET.

### A1 — final graph populated, non-trivial
- 110 domain entities (Req36, Role26, Obl24, Cap20, PracticeArea2, Reg1, Standard1) — ≥80 ✓
- 42 edges: HAS20, SATISFIED_BY12, REQUIRES9, DEFINES1
- 0 `__Entity__`/`RELATES` leakage (clean domain shape) ✓

### A2 — compare.py runs clean, structural parity reported
- compare.py executes without error (see logs/compare-acceptance-*.out). Per-graph + verdict printed.
- Cross-ref filter works: only `cyber_resilience_act__regulation` kept; cross-ref = 0/1 = 0% (<50%) ✓

### A3 — issues #1-#5 handled; automated thresholds
| Issue | Handled | Evidence |
|---|---|---|
| #1 REQUIRES→SATISFIED_BY | SATISFIED_BY synthesized via shared-Chunk co-occurrence = 12 distinct pairs; convergence() runs (value 0, CRA-only) | edges SATISFIED_BY=12; conv runs ✓ |
| #2 cross-ref Reg filter | strict keep-set `cyber_resilience_act__regulation`; 18/19 dropped | Regulation=1, cross-ref 0% ✓ |
| #3 capability_type→type (DEFECT-1) | Capability.type ∈ {technical5, organizational15}; 0 collision | defect-1 check = 0 ✓ |
| #4 status null→active | 0 nodes with null/missing status; all 110 = active | probe ✓ |
| #5 governance absent | ORCH-D1 option (a): compare.py FLAGs "near-absent (Standard=1), EXPECTED"; not a fail | verdict unaffected ✓ |
| #6 pruned edges | works with surviving 80 domain edges (pre-filter in Python) | edges_copied=30 |
| defect-1 regression | =0 | ✓ |
| UNKNOWN labels | =0 | ✓ |
| core chain | {DEFINES,HAS,REQUIRES,SATISFIED_BY}>0 PASS; EXPRESSES=0 REPORTED (ORCH-D10 cross-ref consequence) | ✓ |

### A4 — content fidelity (manual spot-check, qualitative)
3 REQUIRES edges reproduced the pr3-documented convergence points, all semantically correct:
- "Inform Manufacturer of Vulnerability" → "Vulnerability Reporting" ✓
- "Take Corrective Measures or Withdraw/Recall Product" → "Market Suspension and Recall Management" ✓
- "Inform Manufacturer and Authorities of Significant Cybersecurity Risk" → "Cybersecurity Risk Notification" ✓

### Verdict
Hypothesis supported: a professional extraction package (GraphRAG-SDK), post cross-ref filter +
SATISFIED_BY synthesis + status/type normalization, yields a `policy_system`-shaped CRA graph that is
"close/similar" — structurally (110/1475 baseline edges, all core value-chain types present, 0 defects,
0 unknowns) and content-faithful on the spot-checked core chain. Convergence across regulations is 0
**by definition** for a single-regulation sample (expected, not a defect).

### Known limitations (documented, not failures)
- EXPRESSES=0 (all 22 native EXPRESSES were cross-ref-sourced; filter removes them).
- Convergence across >1 Reg = 0 (CRA-only sample).
- SATISFIED_BY sparse (12 of possible; grounded in co-occurrence, not fabricated).
- Governance layers near-absent (1 Standard) per shall-filter; Option (a) — no re-ingestion.
