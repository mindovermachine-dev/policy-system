# © 2026 Cartman ApS. All rights reserved.
# PROGRESS — pipeline-rag3  (CRA-only GraphRAG-SDK native-graph ingestion)

Tracker kept at natural checkpoints. Times dual-stamped: UTC + CPH.
Signal pings to +4553860041 after each checkpoint.

## Mission
Implement pipeline-rag3 to meet/exceed ACs; autonomous; delegate to pi sub-agents;
iterate until ACs met or 8h elapses (window: 20:22–04:22 CPH).

## Checkpoints
| # | UTC    | CPH     | What | Result | Next |
|---|--------|---------|------|--------|------|
| 0 | 18:22  | 20:22   | Ground env; clone venv; stand up tracker | env ready | author source; load baseline |
| 1 | 19:11  | 21:11   | ingest.py authored+verified; baseline loaded; run_worker.sh PROVEN | ingest.py good; baseline 776n/1475e | Azure config; dry run |
| 2 | 20:05  | 22:05   | Plan + critic + fix sub-agents; review pr2 PROGRESS | Model strategy settled | dry run; scale |
| 3 | 20:20  | 22:20   | dry run (6s,0err); --substantive 15 (294s,47n/93r,0err); compare.py | AC1/AC2 MET; AC3 analysis written | run --substantive 30 |
| 4 | 20:47  | 22:47   | --substantive 30 run; automated bar; content spot-check; LEARNINGS+PROGRESS complete | 160n/318e; 5/6 bar pass; extraction quality HIGH | user AC3 call |
| 5 | 21:06  | 23:06   | Final integrity checks, LEARNINGS/PROGRESS updated, Signal sent | Session complete | await user AC3 decision |

## ACs
- **AC1 MET**: 160n/318e non-trivial graph in `policy_system_graphrag_native`
     (30-chunk --substantive --spread; 0 errors).
- **AC2 MET**: `compare.py` run cleanly; output saved to `logs/compare-*.out.txt`.
- **AC3 READY**: full analysis in `docs/ac3-analysis.md`. Recommendation: PROCEED TO RAG4.
     User manual call still pending.

## Autom. bar (for rag4)
`min_domain_entities=80, min_core_chain_types=4, defect1_regressions=0, max_llm_json_failures_pct=10`
→ 30-chunk run: **5/6 PASS.** 1 fail (cross-ref Regs=94% >50%) → out-of-scope for rag3.

## Decisions
- D1 baseline = `tools/graph-ingestion/load_all.sh`. DONE (776n/1475e).
- D2 Azure: BASE=`https://policy-system-graphrag-spike.openai.azure.com/`,
     VERSION=`2024-10-21`, KEY=`az ... --query key1 -o tsv | tr -d '\n'` (84-char).
- D3 venv = clone pr2/.venv. Source self-contained.
- D4 1-chunk dry run (6.05s, 0 err). DONE.
- D5 venv strategy settled.
- D6 model strategy: `qwen3-coder-next:q4_K_M` for file-write; `qwen2.5-coder:14b`
     broken for pi (ctx truncation); `glm-4.7-flash:q8_0` default in run_worker.sh.
- D7 FILTER-REGEX bug (`/shall/i` → 0 matches) FIXED via `_normalise_regex()` in
      ingest.py + improved help text.
- D8 30-chunk automated bar: 5/6 pass; cross-ref Regs 94% fail is out-of-scope.
     Recommendation: PROCEED TO RAG4.

## Ingestion runs
| Run | Params | Time | Nodes | Rels | Errors |
|-----|--------|------|-------|------|--------|
| dry-run | --max-chunks 1 | 6s | 2 | 1 | 0 |
| filter-bug | --substantive 15 --filter-regex '/shall/i' | 2s | 1 | 0 | 0 (0 matched) |
| run-15 | --substantive 15 --spread | 294s | 47 | 93 | 0 |
| run-30 | --substantive 30 --spread | ~480s | 160 | 318 | 0 |

## Deliverables on disk
- `ingest.py` (324 lines, CRA-only, native-graph, _normalise_regex fix)
- `schema.py` (294 lines, capability_type fix)
- `ratelimit.py` (442 lines, RateLimitedLLM ABC-complete)
- `compare.py` (101 lines, correct graph defaults)
- `graphrag-sdk-configuration.md` (reviewed; not changed)
- `docs/ac3-analysis.md` (full AC3 data + recommendation + content spot-check)
- `logs/ingest-*.jsonl` per run; `logs/compare-*.out.txt`; `logs/<ts>-*.out/.err`

## Open items (non-blocking)
- [ ] AC3: user decision (data ready; recommendation: PROCEED TO RAG4).
- [ ] OPTIONAL: full-corpus ingestion (266 chunks, no --substantive cap) —
     expensive (~1h at 10/60s Azure rate limit), overkill for a spike.
- [ ] rag4: transform.py (cross-ref Reg filter, REQUIRES→SATISFIED_BY remap,
      regulation_map.json, governance-layer expansion).

## Status
CLOSED at checkpoint 5. 2h44m elapsed of 8h budget. AC1+AC2 MET.
AC3 analysis complete, on disk, in docs/ac3-analysis.md.
Recommendation: PROCEED TO PIPELINE-RAG4.
