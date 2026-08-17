# © 2026 Cartman ApS. All rights reserved.
# PROGRESS — pipeline-rag5 (full-corpus audited GraphRAG-SDK → CRA parity vs policy_system_cra)

## Status: CLOSED — all ACs PASS (~3h of 8h budget, 2026-08-17)

## Checkpoints
| # | UTC      | CPH      | What | Result |
|---|----------|----------|------|--------|
| 0 | 09:45    | 11:45    | Ground env; DB check | env ready |
| 1 | 10:00    | 12:00    | Plan agent → docs/plan.md | 1065L, 6 BLOCKERs |
| 2 | 10:09    | 12:09    | Critique agent → docs/critique.md | 3B+3C+4M flaws |
| 3 | 10:15    | 12:15    | ORCH fixed BLOCKERs; all 5 files import-clean | OK |
| 4 | 10:18    | 12:18    | Full ingest started (266 chunks, Azure gpt-5.4-mini) | ~1.5h |
| 5 | 12:08    | 14:08    | Ingestion Steps 1-9 done; 818 domain nodes written | OK |
| 6 | 12:16    | 14:16    | Map → 686n/285e final_full (after escaping fix) | OK |
| 7 | 12:38    | 14:38    | compare.py AC-3 PASS; attribution verdict emitted | PASS |
| 8 | 12:43    | 14:43    | Content spot-check written | done |
| 9 | 12:54    | 14:54    | AC re-verification; session compressed | CLOSED |

## AC Results
- **AC-1 PASS**: native_full 1085n/2609e; 815-entry pruned sidecar; 0 errors
- **AC-2 PASS**: final_full 686n/285e; defect-1=0; 0 __Entity__/RELATES leak; 135 SATISFIED_BY
- **AC-3 PASS**: 686 domain entities; core chain OK; 0 unknown labels; no xref gate
- **AC-4 PASS**: β-dominant (129 recoverable vs 1 α vs 0 γ)
- **AC-5 PASS**: qualitative spot-check in docs/content-spotcheck.md

## Verdict (deliverable)
**SDK IS a viable extractor.** Gap is β-dominant: 767 pattern_mismatch prunes +
41 dangling filter drops = 818 total. The extraction succeeded but the SDK's
ontology-conformance step filtered the signal. Fixing pattern directions
(capability→practicearea instead of practicearea→capability, etc.) would
recover most of the gap. No main `cyber_resilience_act__regulation` node was
extracted (open extraction ≠ curated extraction), so 0 DEFINES/EXPRESSES and
the SDK covers a different CRA section (Annex II) than the reference (Art. 13-14).

## Bugs fixed by ORCH
| # | Issue | Severity |
|---|---|---|
| FLAW-001 | Wrong SDK import path | BLOCKER |
| FLAW-002 | GraphData._document_id doesn't exist | BLOCKER |
| FLAW-003 | File opened per-write | BLOCKER |
| FLAW-004 | Original methods lost from scope | CRITICAL |
| NEW-001 | r.rel_type doesn't exist on GraphRelationship | BLOCKER |
| NEW-002 | FalkorDB rejects `''` escaping; use `\'` | BLOCKER |
| NEW-003 | Sub-agent 5-space indentation | MED |
| NEW-004 | Azure embedding endpoint hangs | MED |
| NEW-005 | DEFINES/EXPRESSES in core check = expected 0 | LOW |

## Sub-agent summary
| Agent | Model | Outcome | Time |
|---|---|---|---|
| plan | qwen3-coder-next:q4_K_M | FINISHED/CREATED | ~5min |
| critique | qwen3-coder-next:q4_K_M | FINISHED/CREATED | ~2min |
| impl-ingest | qwen3-coder-next:q4_K_M | FINISHED/CHANGED | ~2min |
| impl-compare | qwen3-coder-next:q4_K_M | FINISHED/CHANGED | ~1min |
| impl-attribution | qwen3-coder-next:q4_K_M | FINISHED/CREATED | ~2min |

All 5 sub-agents produced artifacts, but all required ORCH fixes to be
functionally correct. Sub-agents produce plausible-looking code with real bugs.
