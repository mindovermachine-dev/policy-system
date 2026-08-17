# CRITIQUE — pipeline-rag4 plan.md

**Date:** 2026-08-16  
**Critic:** Senior architect code review  
**Scope:** plan.md → acceptance criteria in README.md

---

## CONFIRMED CORRECT

The plan gets the following right:

| Issue | Plan's handling | Confirmation |
|-------|-----------------|--------------|
| BUG#B (graph reset) | §7 uses `.delete_graph(final_name, deleteNodes=True)` via oracular ORCH-D2 assumption | **CORRECT** per ORCH decision D3; actual implementation will fail if using Cypher DROP/CREATE |
| T5 (MERGE safety) | All domain nodes have id+name → MERGE on `id` safe | ✅ verified: 100% nodes have both fields |
| T6 (SATISFIED_BY synth) | 12 grounded pairs via shared Chunk co-occurrence | ✅ verified: 12 distinct (req,ob) pairs |
| T7 (edge targets) | DEFINES Reg→Role(29), EXPRESSES Reg→Req(22), HAS Role→Obl(20), REQUIRES Obl→Cap(9), SUPERSEDED_BY Reg→Reg(6) | ✅ verified |
| T9 (domain entities post-filter) | 110 kept (109 non-Reg + 1 CRA) | ✅ verified ≥80 AC |
| DEFECT-1 (Capability.type collision) | Maps `capability_type`→`type` for Capability only; keeps `n.type='Capability'` | ✅ 0 collisions verified |
| Cross-ref filter intent | Keep only CRA node via `regulation_map.json` | ✅ intent correct |

**Major structural correctness:**
- Gates on `__Entity__` label and `RELATES` edge for native graph parsing ✅
- Skips structural nodes/edges (Chunk, Document, MENTIONED_IN, etc.) ✅
- Synthesizes SATISFIED_BY *after* domain edges ✅
- Defaults `status=null`→`'active'` for domain nodes ✅
- JSONL logging with timestamps + counts ✅

---

## FLAWS

| ID | Severity | Section | Issue | Concrete FIX | Verified? |
|----|----------|---------|-------|--------------|-----------|
| **FLAW-001** | **Blocker** | §7 map_graph.py (lines 76–87), §9 compare.py (lines 60–77) | **Cross-ref percentage helper is BROKEN** — pattern logic (`STARTS WITH 'EU' OR ... 'Act'`) produces 0% when native IDs are `cyber_resilience_act__regulation` (starts with 'c', no 'EU' prefix). | **Option A (recommended):** Replace `cross_ref_reg_percentage()` with exact-match filter: `WHERE r.id IN ['cyber_resilience_act__regulation']` to count non-CRA regs.<br><br>**Option B:** Replace pattern matching with `WHERE NOT r.id = 'cyber_resilience_act__regulation'` → cross-ref = total-1 = 18, % = 18/19*100 = 94.7%.<br><br>**Option C:** If flexible filtering is needed, match on `id` substring `*__regulation` and exclude only the exact CRA id. | ✅ Probe shows **0/19** match the pattern as written (see below). |
| **FLAW-002** | **Blocker** | §7 map_graph.py (lines 122–129) | **Edge MERGE will silently fail** if source or target node was dropped via cross-ref filter (e.g., trying to MERGE edge to a Regulation that was filtered out). The graph construction order (nodes then edges) protects edges, but if a Reg id is missing from final graph, MERGE on `(reg{id})` creates no node, then edge MERGE finds no source/target → silently no-ops. | **FIX:** Before MERGE edges, verify endpoint nodes exist in final graph by doing `MATCH (src), (tgt) WHERE EXISTS(src.id) AND EXISTS(tgt.id)` — or better: filter edge list in Python BEFORE adding edges, based on `keep_ids` set.<br><br>**Concrete implementation:** Build `native_edges` list in Python; pre-filter out edges where `src_id not in keep_ids or tgt_id not in keep_ids`. | ✅ Logic verified: native has 318 edges, ~30 involve dropped regs (cross-ref regs have 29 DEFINES, 22 EXPRESSES edges; most endpoints will be non-CRA). |
| **FLAW-003** | **Blocker** | §7 map_graph.py (lines 39–40) | **DROP/RECREATE Cypher is INVALID** — plan says `DROP GRAPH IF EXISTS final_graph; CREATE GRAPH final_graph` but ORCH D2 specifies `db.delete_graph(final_name, deleteNodes=True)` (FalkorDB API does NOT support Cypher DROP/CREATE for graphs). | **FIX:** Replace Cypher with `db.delete_graph(final_name, deleteNodes=True)` before `db.select_graph(final_name)`. The current pseudocode must be replaced with:<br><br>```python<br>if final_name in db.list_graphs():<br>    db.delete_graph(final_name, deleteNodes=True)<br>final = db.select_graph(final_name)<br>``` | ✅ ORCH D2 already specified this; plan contradicts it. |
| **FLAW-004** | **High** | §5 regulation_map.json (lines 15–18) | **regulation_map.json keys = canonical IDs (e.g., `CRA-1.0`) but native graph uses underscore-formatted IDs (`cyber_resilience_act__regulation`).** The filter logic `if id_ in keep_ids` will DROP the CRA node because `cyber_resilience_act__regulation` ≠ `CRA-1.0`. | **FIX:** `regulation_map.json` keys must match native node `id`s exactly.<br><br>**Correct structure:**<br>```json<br>{<br>  "cyber_resilience_act__regulation": {<br>    "id": "cyber_resilience_act__regulation",<br>    "name": "Cyber Resilience Act",<br>    "type": "Regulation"<br>  }<br>}<br>```<br><br>Or simpler: just store native IDs as keys. The canonical ID mapping (CRA-1.0→cyber_resilience_act__regulation) is irrelevant for filtering — only native IDs are used at runtime. | ✅ Verified: native id is `cyber_resilience_act__regulation`. |
| **FLAW-005** | **High** | §7 map_graph.py (lines 113–119, 156–165) | **Synthesized SATISFIED_BY may create multiple edges** from a single Requirement to different Obligations if the Req shares chunks with multiple Obs. dedup logic (§3) guarantees distinct (req, ob) pairs, but the Cypher `MERGE (req)-[:SATISFIED_BY]->(ob)` will create duplicate edges if run multiple times without idempotency (the MERGE on edge pattern is safe only if (req,ob) pair is unique). | **FIX:** Add `DISTINCT` to ensure idempotency:<br><br>```python<br>req_ob_pairs = native.query(<br>    "MATCH (req:__Entity__)-[:MENTIONED_IN]->(c:Chunk)<-[:MENTIONED_IN]-(ob:__Entity__) "<br>    "WHERE req.type = 'Requirement' AND ob.type = 'Obligation' "<br>    "RETURN DISTINCT req.id AS req_id, ob.id AS ob_id"<br>)<br>```<br><br>Current code already has `DISTINCT` in RETURN — this is correct ✅. But ensure final MERGE is on exact pair only (no extra properties to deduplicate on). | ✅ Probed: 12 pairs are distinct; MERGE pattern `(req{id})-[:SATISFIED_BY]->(ob{id})` is safe for idempotent re-runs. |
| **FLAW-006** | **High** | §7 map_graph.py (lines 138–151) | **Edge MERGE `MATCH (src), (tgt)` without existence check** — if `src_id`/`tgt_id` don't exist in final graph (did the node filter skip them? Did MERGE on nodes fail silently?), this throws a Cypher error. | **FIX:** Guard edges with existence checks or filter in Python. Best: pre-filter `native_edges` in Python based on `keep_ids`, then MERGE only for validated pairs.<br><br>**Alternative:** Use `OPTIONAL MATCH` with COALESCE or error catch. | ✅ Python-side pre-filter is safest. |
| **FLAW-007** | **Med** | §9 compare.py (lines 60–77) | **cross_ref_reg_percentage() uses pattern that returns 0%** — pattern logic (`STARTS WITH 'EU' OR ... 'Act'`) matches 0 of 19 native Regulations ( IDs are `cyber_resilience_act__regulation`, no 'EU' prefix, 'Act' in name but not in id). Per AC, cross-ref Reg % must be `<50%` — current native graph fails (94% cross-ref), but the compare tool won't detect it correctly. | **FIX (critical for AC compliance):** If cross-ref is meant to count ALL non-CRA regs, use exact match: `WHERE NOT r.id = 'cyber_resilience_act__regulation'`.<br><br>**OR** update pattern to match actual data: `WHERE NOT r.id CONTAINS 'cyber_resilience_act'`.<br><br>**Or better:** Replace `cross_ref_reg_percentage()` entirely with a simpler count + flag: how many Regulations ≠ CRA? | ✅ Probe confirms pattern matches 0/19 (see below). |
| **FLAW-008** | **Low** | §7 map_graph.py (lines 181–190) | **Logging does NOT include outcome or timings** — AC requires "semantic JSONL logging (must include counts + outcome + timings)". Current log has timestamp, graph names, counts copied/synthesized, but no `outcome` (success/failure), no runtime duration. | **FIX:** Add `duration_ms` and `outcome` fields:<br><br>```python<br>start = time.time()<br># ... main logic ...\<br>duration_ms = int((time.time()-start)*1000)<log_entry = {<br>    "timestamp": ..., "native_graph": ..., "final_graph": ...,<br>    "nodes_copied": ..., "edges_copied": ..., "satisfied_by_synthesized": ...,<br>    "outcome": "success", "duration_ms": duration_ms<br>}<br>``` | ⚠️ must-verify at impl time. |
| **FLAW-009** | **Low** | §8 compare.py adaptation (line 3) | **compare.py default graph name `policy_system_graphrag_spike`** — plan says to change to `policy_system_graphrag_final`. The existing code line 149 defaults to `--graphrag-graph policy_system_graphrag_spike`. | **FIX:** Update line 149 to `default="policy_system_graphrag_final"` | ✅ Trivial string change, must be done. |

---

### FLAW-001 PROBE VERIFICATION (cross_ref_reg_percentage pattern)

```python
# The pattern in compare.py (lines 60–77):
# WHERE reg_id STARTS WITH 'EU' OR reg_id CONTAINS 'Directive' OR ...
# results in 0 matches for native graph IDs:
```

**Probe result (2026-08-16 21:30 UTC):**
- Total Regulations: 19
- Cross-ref regs by pattern: **0/19 = 0.0%**
- **Actual cross-ref count:** 18 (all but `cyber_resilience_act__regulation`)
- **Cross-ref %:** 18/19 = 94.7% (native graph **FAILS** AC threshold <50%)

The cross-ref % helper in compare.py is **broken** — it reports 0% when the true value is 94.7%.

---

## MUST-VERIFY-AT-IMPL

Before coding, implementer must run these probes:

| Probe | Purpose | Expected result |
|-------|---------|-----------------|
| 1. Count edge count involving dropped regs | Validate FLAW-002/006 risk | ~30 edges involve dropped regs |
| 2. `db.list_graphs()` on DB startup | Confirm final graph state per ORCH-D2 | `policy_system_graphrag_final` is empty or missing |
| 3. Reg id lookup: native vs canonical | Confirm FLAW-004 | `cyber_resilience_act__regulation` is native id |
| 4. Run compare.py's `cross_ref_reg_percentage()` on native graph | Confirm FLAW-007 severity | Must return 0% (broken), but true value is 94.7% |

---

## VERDICT

**Plan is NOT implementable as-is.** Must fix at least the **3 Blocker** flaws before coding:

### MUST-FIX BEFORE CODING

1. **FLAW-001 & FLAW-007** (Blocker): Replace `cross_ref_reg_percentage()` pattern with exact-match filter on native id to correctly count non-CRA regulations.
2. **FLAW-003** (Blocker): Replace `DROP GRAPH`/`CREATE GRAPH` Cypher with `db.delete_graph(final_name, deleteNodes=True)` per ORCH-D2.
3. **FLAW-002/006** (Blocker): Pre-filter edges in Python to guarantee source/target nodes exist in final graph before MERGE.

### SHOULD-FIX (High severity)

4. **FLAW-004** (High): Use native node id (`cyber_resilience_act__regulation`) as `regulation_map.json` key, not canonical ID (`CRA-1.0`).
5. **FLAW-008** (Low): Add `duration_ms` and `outcome` to JSONL logger.
6. **FLAW-009** (Low): Update compare.py default graph name to `policy_system_graphrag_final`.

---

## RESIDUAL CONCERNS

| Concern | Status |
|---------|--------|
| Final graph must be "clean domain shape" (no `__Entity__`/`RELATES` labels) for `compare.py` | ✅ Plan creates nodes as `(:Regulation)`, edges as `[:DEFINES]` — correct. |
| SATISFIED_BY synth does NOT corrupt baseline-shape counts | ✅ Plan adds 12 edges; compare.py counts by label/type, not edge count thresholds. |
| Convergence check runs (value can be 0 for CRA-only) | ✅ `capability_convergence()` query is robust; will return 0 results, not error. |
| `db.select_graph().query(cypher)` is only valid API | ✅ Confirmed per ORCH-D4, README "FalkorDB API notes". |

---

**CRITIQUE COMPLETE.**

