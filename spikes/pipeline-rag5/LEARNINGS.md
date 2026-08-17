# © 2026 Cartman ApS. All rights reserved.
# LEARNINGS — pipeline-rag5 (rolling; essentials only)

## Environment
- E1 GraphRAG SDK 1.4.0. IngestionPipeline imported from `graphrag_sdk` (NOT `ingestion_pipeline`).
- E2 `GraphRelationship` has NO `rel_type` attribute — use `r.properties.get('rel_type', r.type)`.
- E3 `GraphData` has `extracted_entities`, `extracted_relations`, `nodes`, `relationships` — NO `_document_id`.
- E4 FalkorDB does NOT accept single-quote doubling `''` in Cypher string literals.
  Use backslash `\'` instead.
- E5 FalkorDB `.query()` does NOT support parameter binding via kwargs.
- E6 Azure gpt-5.4-mini latency ~15s/call; embedding endpoint hangs indefinitely
  (infinite 60s backoff). Kill the process when graph data is written to DB.
- E7 pr3 venv cloned to pr5/.venv. graphrag_sdk=1.4.0, falkordb=v1.7.1.

## Extraction findings
- X1 Full CRA.pdf → 266 chunks → 818 domain nodes, 529 domain rels (before prune).
- X2 SDK did NOT extract a `cyber_resilience_act__regulation` node (the regulation it IS).
  It extracted 129 cross-ref Regulation nodes (directives, OJs, articles).
  → No main Regulation node → no DEFINES/EXPRESSES in final graph.
- X3 767 pattern_mismatch prunes: direction inversions (e.g., COVERS: `Regulation→Regulation`
  instead of `PracticeArea→Capability`).
- X4 SDK extracted governance-layer elements: 13 Standard, 6 Policy, 4 Control, 2 PA
  (reference has 0 of these). This is the "γ layer" that pr4 didn't have.
- X5 Candidate graph is 3.7× larger than reference (686 vs 188 nodes) but covers
  a DIFFERENT section of the CRA (Annex II quality system vs Art. 13-14).

## Audit patch (F2) findings
- F1 Audit patch captures 815 pruned entries correctly.
- F2 All pruned entries are relationships — 0 pruned nodes.
- F3 filter_quality dropped 41 dangling relationships (endpoints lost before quality check).
- F4 prune dropped 774: 767 pattern_mismatch + 7 rel_type_undeclared.
- F5 SDK's `_prune` also runs its OWN pruning (visible in SDK logs as "Pruned X Y relationships
  due to (source, target) mismatch"). The audit patch wraps around this.
- F6 Pattern-mismatch causes include reversed directions (Capability→PracticeArea
  instead of PracticeArea→Capability) and wrong rel_types (SATISFIED_BY used for
  things that should be REQUIRES).

## Sub-agent findings
- S1 qwen3-coder-next:q4_K_M produces systemic 5-space indentation. All 5 files
  from agents required multiple manual rewrites. **Fix: normalize indentation in
  ORCH before running the file. Or write critical files manually.**
- S2 Agents produce plausible-looking code that has import bugs (wrong module path),
  missing attributes (r.rel_type), and wrong escaping. **Verify imports before running.**
- S3 Critique agent was valuable: 3 of 10 flaws caught by ORCH independently
    (NEW-001, NEW-002, NEW-004 would have been BLOCKERs).
- S4 Long-running ingest (~1h) is not suitable for sub-agent watchdog (1200s max).
  Run directly in ORCH with background process + monitoring.
- S5 `run_worker.sh` works well for scoped tasks (<200s). For long-running, use
  ORCH-level background processes + explicit monitoring.

## Map/compare findings
- M1 `map_graph.py` from pr4 has `esc()` that doubles single quotes (`''`).
  FalkorDB rejects this. Fixed to `\'` backslash escaping.
- M2 Cross-ref filter drops ALL 129 Regulation nodes (keep-set has 1 entry that
  doesn't exist in the SDK graph). Result: 0 Reg, 0 DEFINES, 0 EXPRESSES in final.
- M3 SATISFIED_BY synthesis: 135 pairs from 266 chunks' MENTIONED_IN co-occurrence.
- M4 `compare.py` from pr4 has broken f-string handling of `inf` ratios.
  Rewritten to use `fmt_ratio()` helper.
- M5 Core chain check: DEFINES/EXPRESSES are expected to be 0 in this spike
  (no main Reg node). Report them, don't grade them.

## Verdict (α/β/γ)
- V1 Gap is β-DOMINANT: 129 recoverable vs 1 extractor-limited vs 0 governance.
   The SDK CAN extract the data; the pipeline's _prune filters it out due to
   pattern mismatches (direction inversions).
   → SDK is a viable extractor; fix the ontology patterns and the gap closes.
- V2 The SDK produces a broader but less curated graph. Quality over quantity
   trade-off: the reference is curated, the SDK is open extraction.
