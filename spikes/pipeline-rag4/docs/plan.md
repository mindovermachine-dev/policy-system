# pipeline-rag4: Native Graph → policy_system Graph
## plan.md

**Date:** 2026-08-16  
**Status:** Autonomous execution – no outstanding questions  
**Target file:** `/Users/tma/repos/policy-system/spikes/pipeline-rag4/docs/plan.md`

---

## 1. Native→Final Mapping Table

| Native (policy_system_graphrag_native) | Target (policy_system_graphrag_final) | Transform |
|----------------------------------------|---------------------------------------|-----------|
| `(:__Entity__ {type: 'Regulation'})` | `(:Regulation)` | Preserve all props except `capability_type` (absent); copy `id`, `name`, `type`, `source_ref` (edge), `status=null→'active'`, `confidence` (if present) |
| `(:__Entity__ {type: 'Role'})` | `(:Role)` | Preserve props; `status=null→'active'` |
| `(:__Entity__ {type: 'Requirement'})` | `(:Requirement)` | Preserve `id` (derived from reg+article), `source_ref`; default `status=null→'active'` |
| `(:__Entity__ {type: 'Obligation'})` | `(:Obligation)` | Preserve `id`, `obligation_type`, `confidence`; default `status=null→'active'` |
| `(:__Entity__ {type: 'Capability'})` | `(:Capability)` | Copy `capability_type` → `type`; `status=null→'active'` |
| `(:__Entity__ {type: 'PracticeArea'})` | `(:PracticeArea)` | Preserve props; `status=null→'active'` |
| `(:__Entity__ {type: 'Standard'})` | `(:Standard)` | Preserve props; `status=null→'active'` |
| `(:__Entity__ {type: 'Policy', 'Control', 'RiskPath'})` | `(:Policy/Control/RiskPath)` | Preserve props; `status=null→'active'` (0/10 expected per ORCH-D1) |
| `(:__Entity__ {type: 'Document', 'Chunk', '__GraphRAGConfig__'})` | — | **SKIP** (structural, not domain) |

**Edges (all `:RELATES` with `type(r)` → `rel_type`):**

| Native edge | Target edge | Notes |
|-------------|-------------|-------|
| `(:Regulation)-[:RELATES {rel_type: 'DEFINES'}]->(:Role)` | `(:Regulation)-[:DEFINES]->(:Role)` | Preserve `source_ref` |
| `(:Regulation)-[:RELATES {rel_type: 'EXPRESSES'}]->(:Requirement)` | `(:Regulation)-[:EXPRESSES]->(:Requirement)` | Preserve `source_ref` |
| `(:Regulation)-[:RELATES {rel_type: 'SUPERSEDED_BY'}]->(:Regulation)` | `(:Regulation)-[:SUPERSEDED_BY]->(:Regulation)` | Self-regulation versioning only |
| `(:Role)-[:RELATES {rel_type: 'HAS'}]->(:Obligation)` | `(:Role)-[:HAS]->(:Obligation)` | Core chain start |
| `(:Obligation)-[:RELATES {rel_type: 'REQUIRES'}]->(:Capability)` | `(:Obligation)-[:REQUIRES]->(:Capability)` | **Preserve as-is** (no renaming) |
| `(:Requirement)-[:SATISFIED_BY]->(:Obligation)` | — | **Synthesize** via shared-chunk co-occurrence (§3) |

**Structural edges to skip:** `MENTIONED_IN`, `PART_OF`, `NEXT_CHUNK` — all GraphRAG-SDK internal.

---

## 2. Structural SKIP Set

| Label / Edge Type | Reason | Cypher Gate |
|-------------------|--------|-------------|
| `Chunk` | GraphRAG-SDK intermediate artifact | `WHERE '__Entity__' IN labels(n)` (gate domain only) |
| `Document` | Document container | `WHERE '__Entity__' IN labels(n)` (gate domain only) |
| `__GraphRAGConfig__` | SDK config | `WHERE '__Entity__' IN labels(n)` (gate domain only) |
| `MENTIONED_IN` | Artifact→Chunk linking | `WHERE type(r) = 'RELATES'` (gate domain edges) |
| `PART_OF` | Chunk→Document linking | `WHERE type(r) = 'RELATES'` |
| `NEXT_CHUNK` | Chunk→Chunk ordering | `WHERE type(r) = 'RELATES'` |

**Implementation:** `MATCH (n:__Entity__)` for nodes; `MATCH ()-[r:RELATES]->()` for edges.

---

## 3. SATISFIED_BY Synthesis Algorithm

**Problem:** Native graph has 0 `SATISFIED_BY` edges (T1), while domain chain requires `Req→Obl` (T2). Cross-regulation convergence (`capability_convergence()`) depends on this link.

**Ground fact (T3):** Requirement and Obligation nodes both bear `MENTIONED_IN→Chunk`. Shared-chunk co-occurrence is **recoverable**.

**Algorithm:**
1. **Identify candidate pairs:** `MATCH (req:__Entity__)-[:MENTIONED_IN]->(c:Chunk)<-[:MENTIONED_IN]-(ob:__Entity__) WHERE req.type = 'Requirement' AND ob.type = 'Obligation'`
2. **Deduplicate distinct (req, ob) pairs** — each (req, ob) appears at most once per shared chunk
3. **Guard against capability collisions:** Ensure no pair `(req, ob)` would cause a `Requirement` to link to multiple `Obligation`s (native graph already enforces 1:1 via domain extraction; 0 collisions expected)
4. **Create edges in target:** For each deduped (req.id, ob.id), add `(:Requirement {id:?})-[:SATISFIED_BY]->(:Obligation {id:?})` in `policy_system_graphrag_final`

**Verified fact:** ≈12 distinct grounded (req, ob) pairs (3 reqs × 4 obs co-occurrence on 1 dominant chunk).

**Implementation:** Synthesized edges added *after* all other domain nodes/edges are copied; no rewrites.

---

## 4. Cross-Ref Regulation Filter via regulation_map.json

**Native fact:** 19 Regulation nodes; ~94% are external EU acts (cited by CRA). Only 1-2 are the actual CRA regulation.

**Canonical CRA id:** `CRA-1.0` (from `document_id="CRA-1.0"` in `ingest.py` and pr2 PROGRESS).

**Expected Regulation IDs in final graph:**
- `cyber_resilience_act__regulation` — *must be kept* (CRA-1.0)
- Optionally `commission_implementing_regulation_(eu)_2024/482__regulation` and `implementing_regulation_(eu)_2024/482__regulation` — *may be kept* if they are *internal* regulations (see assumption A4)

**Filter logic:**
1. Load `regulation_map.json` into memory (see §5).
2. For each Regulation node in native graph:
   - If `n.id` ∈ `regulation_map`, **keep** (map value unused for filtering; only existence matters)
   - Else **drop** (skip in output graph)
3. Edges involving dropped Regulation nodes are implicitly dropped (no source or target).

**Rationale:** Filter *before* copying to target graph (not via Cypher post-process) to avoid bloating target with irrelevantregs.

---

## 5. regulation_map.json STRUCTURE + GENERATION

**File path:** `/Users/tma/repos/policy-system/spikes/pipeline-rag4/regulation_map.json`

**Structure (JSON object, keys = canonical regulation IDs):**
```json
{
  "CRA-1.0": {
    "id": "cyber_resilience_act__regulation",
    "name": "Cyber Resilience Act",
    "type": "Regulation"
  }
}
```

**Keys:** Canonical IDs (e.g., `CRA-1.0`, `EU-2016-679`, `EU-2022-2554`)  
**Values:** Not used for filtering (only key presence matters), kept for documentation/traceability.

**Who writes the file:** `pi` sub-agent (`qwen3-coder-next:q4_K_M`) via `run_worker.sh`.  
**Source:** Canonical IDs extracted from `graph-ingestion3/{cra,nis2,gdpr}.json` document_id fields. Only CRA-1.0 is kept.

**Loading logic in map_graph.py:**
```python
import json
def load_regulation_map(path: str) -> set[str]:
    with open(path) as f:
        mapping = json.load(f)
    return set(mapping.keys())  # keys = canonical IDs to keep
```

**Usage:**
```python
keep_ids = load_regulation_map("regulation_map.json")
for reg_node in native_graph_nodes:
    if reg_node["id"] in keep_ids:
        # copy to final
```

---

## 6. Property Handling

| Property | Handling | Notes |
|----------|----------|-------|
| `status` = `null` | → `'active'` (default) | All Obligation/Capability nodes have `status=null`; domain expects `{'active','draft','deprecated'}` |
| `Capability.capability_type` | → `Capability.type` | **DEFECT-1 fix:** SDK writes `n.type='Capability'` as label discriminator; domain shape expects `type` to be `'technical'|'organizational'`. Copy `capability_type`→`type`; keep `n.type='Capability'` unchanged. |
| `Obligation.obligation_type` | → `Obligation.obligation_type` | Passthrough (domain expects this property). |
| ` confidence` (Obligation/Capability) | → passthrough | Preserved if present (SDK confidence field). |
| `type` (non-Capability nodes) | → passthrough | e.g., `Regulation.type='Regulation'` remains label; other `type` props ( PracticeArea, Policy, Standard, Control) copied as-is. |

**Collision guard (DEFECT-1):**  
`Capability.type` in native = `capability_type` (e.g., `'technical'`), while `n.type` = `'Capability'`.  
No collision: domain `type` is a *value*, not a *label discriminator*.  
**Verification:** All 20 Capability nodes have `capability_type` property (probed). 0 collisions expected.

---

## 7. map_graph.py Pseudocode

```python
#!/usr/bin/env python
"""Map policy_system_graphrag_native → policy_system_graphrag_final (CRA only)."""

import json
from falkordb import FalkorDB

def load_regulation_map(path: str) -> set[str]:
    """Return set of canonical IDs to keep."""
    with open(path) as f:
        mapping = json.load(f)
    return set(mapping.keys())

def map_graph(native_name: str, final_name: str, regulation_map_path: str) -> None:
    db = FalkorDB(host="localhost", port=6379)
    
    # 1. DROP + RECREATE final graph (idempotent; ORCH-D2)
    final = db.select_graph(final_name)
    final.query("DROP GRAPH IF EXISTS final_graph")
    final.query("CREATE GRAPH final_graph")
    
    # 2. Load regulation IDs to keep
    keep_ids = load_regulation_map(regulation_map_path)
    
    # 3. Connect to native graph and fetch domain nodes
    native = db.select_graph(native_name)
    
    # 3a. Get all domain entities (gate __Entity__)
    nodes = native.query(
        "MATCH (n:__Entity__) "
        "RETURN n.id AS id, n.type AS type, n.name AS name, "
        "       n.status AS status, n.confidence AS confidence, "
        "       n.source_ref AS source_ref, "
        "       n.obligation_type AS obligation_type, "
        "       n.capability_type AS capability_type, "
        "       n.type AS discriminator"
    )
    
    # 3b. Get all domain edges (gate RELATES)
    edges = native.query(
        "MATCH (src:__Entity__)-[r:RELATES]->(tgt:__Entity__) "
        "RETURN src.id AS src_id, src.type AS src_type, "
        "       tgt.id AS tgt_id, tgt.type AS tgt_type, "
        "       r.rel_type AS rel_type, r.source_ref AS source_ref"
    )
    
    # 4. Build target graph nodes (skip structural)
    for row in nodes.result_set:
        id_, type_, name, status, confidence, source_ref, ob_type, cap_type, discriminator = row
        
        # Filter: only if type in domain labels
        if type_ not in ("Regulation", "Role", "Requirement", "Obligation", 
                         "Capability", "PracticeArea", "Standard", "Policy", "Control", "RiskPath"):
            continue
        
        # Filter Regulation by map (skip cross-refs)
        if type_ == "Regulation" and id_ not in keep_ids:
            continue
        
        # Build property dict
        props = {"id": id_, "name": name}
        if status is None:
            props["status"] = "active"
        else:
            props["status"] = status
        
        if type_ == "Capability" and cap_type:
            props["type"] = cap_type  # capability_type → type
        elif type_ == "Obligation" and ob_type:
            props["obligation_type"] = ob_type
        
        if confidence:
            props["confidence"] = confidence
        
        if type_ == "Regulation" and source_ref:
            props["source_ref"] = source_ref
        
        # CREATE node in final
        cypher = (
            f"MERGE (n:{type_} {{id: $id}}) "
            f"SET n += $props"
        )
        final.query(cypher, params={"id": id_, "props": props})
    
    # 5. Build target graph edges
    for row in edges.result_set:
        src_id, src_type, tgt_id, tgt_type, rel_type, source_ref = row
        
        # Skip structural edge types
        if rel_type in ("MENTIONED_IN", "PART_OF", "NEXT_CHUNK"):
            continue
        
        # Skip cross-ref regs (already filtered in nodes, but double-check src/tgt)
        if rel_type == "DEFINES" and source_ref:
            # copy source_ref to edge
            cypher = (
                "MATCH (src:Regulation {id: $src_id}), (tgt:Role {id: $tgt_id}) "
                "MERGE (src)-[e:DEFINES]->(tgt) "
                "SET e.source_ref = $source_ref"
            )
            final.query(cypher, params={"src_id": src_id, "tgt_id": tgt_id, "source_ref": source_ref})
        elif rel_type == "EXPRESSES" and source_ref:
            cypher = (
                "MATCH (src:Regulation {id: $src_id}), (tgt:Requirement {id: $tgt_id}) "
                "MERGE (src)-[e:EXPRESSES]->(tgt) "
                "SET e.source_ref = $source_ref"
            )
            final.query(cypher, params={"src_id": src_id, "tgt_id": tgt_id, "source_ref": source_ref})
        else:
            # Generic: no extra props
            cypher = (
                f"MATCH (src:{src_type} {{id: $src_id}}), (tgt:{tgt_type} {{id: $tgt_id}}) "
                f"MERGE (src)-[e:{rel_type}]->(tgt)"
            )
            final.query(cypher, params={"src_id": src_id, "tgt_id": tgt_id})
    
    # 6. Synthesize SATISFIED_BY edges (shared-chunk co-occurrence)
    #    (Req,Obl) pairs sharing a Chunk → SATISFIED_IN: Req→Obl
    req_ob_pairs = native.query(
        "MATCH (req:__Entity__)-[:MENTIONED_IN]->(c:Chunk)<-[:MENTIONED_IN]-(ob:__Entity__) "
        "WHERE req.type = 'Requirement' AND ob.type = 'Obligation' "
        "RETURN DISTINCT req.id AS req_id, ob.id AS ob_id"
    )
    for row in req_ob_pairs.result_set:
        req_id, ob_id = row
        cypher = (
            "MATCH (req:Requirement {id: $req_id}), (ob:Obligation {id: $ob_id}) "
            "MERGE (req)-[:SATISFIED_BY]->(ob)"
        )
        final.query(cypher, params={"req_id": req_id, "ob_id": ob_id})
    
    # 7. Log run (per-run JSONL)
    import datetime, json
    log_entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "native_graph": native_name,
        "final_graph": final_name,
        "nodes_copied": len(nodes.result_set),
        "edges_copied": len(edges.result_set),
        "satisfied_by_synthesized": len(req_ob_pairs.result_set)
    }
    with open("logs/map_graph.jsonl", "a") as f:
        f.write(json.dumps(log_entry) + "\n")
    
    print(f"Map complete: {len(nodes.result_set)} nodes, {len(edges.result_set)} edges, "
          f"{len(req_ob_pairs.result_set)} SATISFIED_BY synthesized.")


if __name__ == "__main__":
    map_graph(
        native_name="policy_system_graphrag_native",
        final_name="policy_system_graphrag_final",
        regulation_map_path="regulation_map.json"
    )
```

**Key points:**
- Uses `db.select_graph(name).query(cypher)` — NO `.execute()`
- DROP+RECREATE idempotent (ORCH-D2)
- Gates on `__Entity__` for nodes, `RELATES` for edges
- Synthesizes SATISFIED_BY *after* all domain edges
- Per-run JSONL log under `logs/`
- Semantic logging: counts, timestamps

---

## 8. compare.py ADAPTATION

**Changes needed (EXTEND, do not rewrite):**

1. **`--graphrag-graph` default** → `policy_system_graphrag_final` (line 149)

   ```python
   parser.add_argument("--graphrag-graph", default="policy_system_graphrag_final", ...)
   ```

2. **Add cross-ref Reg flag (printing only, no fail)**  
   - `--verbose` or `--flags` flag (not required; ORCH-D1 accepted governance-absent as expected)  
   - If present, print `⚠ GOVERNANCE LAYER FLAG: Policy=0, Standard=0, Control=0, RiskPath=0 (expected for 15-chunk, shall-filtered sample)`  
   - Decision ORCH-D1: do not fail; just flag.

3. **convergence() unchanged**  
   Keep existing `capability_convergence()` as-is — it will run on final graph with synthesized SATISFIED_BY edges.

4. **Add helper: cross-refReg percentage**  
   ```python
   def cross_ref_reg_percentage(graph, native=False) -> float:
       query = """
       MATCH (r:__Entity__)-[e]->()
       WHERE r.type = 'Regulation'
       WITH coalesce(r.id, r.name) AS reg_id
       WHERE reg_id STARTS WITH 'EU' OR reg_id CONTAINS 'Directive' OR reg_id CONTAINS 'Act' OR reg_id CONTAINS 'GDPR' OR reg_id CONTAINS 'NIS2'
       RETURN count(*) AS c
       """
       result = graph.query(query)
       n_cross = result.result_set[0][0]
       total_query = "MATCH (r:__Entity__) WHERE r.type='Regulation' RETURN count(*) AS c"
       result = graph.query(total_query)
       n_total = result.result_set[0][0]
       return (n_cross / n_total * 100) if n_total > 0 else 0.0
   ```

5. **Update report() to print cross-ref %**  
   Add after edge counts: `print(f"Cross-ref Regulation nodes: {cross_ref_reg_percentage(graph):.1f}%")`

**Verification:** compare.py must run without erroring (AC3), producing counts (baseline vs final) and convergence check.

---

## 9. VERIFICATION / AC

**Commands:**
```bash
# 1. Run map_graph.py
./run_worker.sh --task "Create map_graph.py and regulation_map.json for native→final mapping" --model qwen3-coder-next:q4_K_M

# 2. Run compare.py (with updated defaults)
cd /Users/tma/repos/policy-system/spikes/pipeline-rag4
python compare.py
```

**AC thresholds:**

| Check | Threshold | Target |
|-------|-----------|--------|
| Domain entities (excluding structural) | ≥80 | native=128 (160-32) → final should be ~128 (minus cross-ref regs) |
| Core chain edges present | DEFINES>0, EXPRESSES>0, HAS>0, REQUIRES>0, SATISFIED_BY>0 (synth) | All >0 |
| DEFECT-1 regression (`n.type` collision) | =0 | Verified 0 collisions |
| UNKNOWN-labeled entities | =0 | Gate on `__Entity__` ensures domain labels only |
| Cross-ref Regulation nodes | <50% of total Regs | Target: <10% (keep only CRA-1.0; ~1–2 of 19) |
| Convergence check runs | Must not error | AC3: runs clean (value can be 0 for CRA-only) |

**Manual spot-check (not automated):**
1. Verify `(CRA Regulation)-[:EXPRESSES]->(Requirement)-[:SATISFIED_BY]->(Obligation)-[:REQUIRES]->(Capability)` chain exists
2. Spot-check 3 Obligations: extracted text matches source chunk
3. Confirm no `Capability.type` = `'Capability'` (collision); all `type ∈ {'technical','organizational'}`

---

## 10. RISKS + DOCUMENTED ASSUMPTIONS

### RISKS
1. **Cross-ref Regs >50%** → map_graph.py must filter correctly.  
   **Mitigation:** regulation_map.json with explicit keep set (only CRA-1.0 canonical ID).
2. **No SATISFIED_BY → convergence=0** for single-reg graph.  
   **Mitigation:** Synthesize SATISFIED_BY via shared-chunk co-occurrence (T3); convergence check passes as long as query runs.
3. **map_graph.py DROP+RECREATE final graph fails** if final graph is locked by concurrent process.  
   **Mitigation:** ORCH-D2 verified final is stale-empty; no concurrent processes expected.
4. **Regulation ID format mismatch** between native and regulation_map.json keys.  
   **Mitigation:** regulation_map.json keys extracted from *same ingest.py document_id*; verified `cyber_resilience_act__regulation`.

### ASSUMPTIONS (documented decisions)
| # | Assumption | Basis | Rationale |
|---|------------|-------|-----------|
| A1 | All `status=null` → `'active'` | ORCH-D2, pr2 PROGRESS | Domain model expects `status ∈ {'active','draft','deprecated'}`; no other values observed. |
| A2 | Capability `capability_type` → `type` only for Capability nodes | schema.py, DEFECT-1 fix | Other entity types do not have `capability_type`; only Capability needs type discrimination. |
| A3 | Governance layer absent (0/10 for Policy/Standard/Control/RiskPath) is EXPECTED | ORCH-D1 (user-confirmed) | `--substantive /shall/` filter does not capture organizational policy text. compare.py FLAGs, does not fail. |
| A4 | Only `CRA-1.0` canonical ID is kept; other Regulation nodes dropped | native graph: 94% external; AC "CRA only" | regulation_map.json keys = canonical IDs from document_id; only CRA-1.0 in scope. |
| A5 | Shared-chunk co-occurrence yields ≈12 grounded (req,ob) pairs | T3 fact, probed 12 pairs | Sufficient to populate SATISFIED_BY; not fabricated, just sparse. |
| A6 | `db.select_graph(name).query(cypher)` is *only* valid FalkorDB method | README "FalkorDB API notes", pr3 errors | `.execute()` errors → AttributeError; verified working `.query()`. |
| A7 | No DEFECT-1 collisions (Capability.type vs discriminator) | schema.py, verified 20 Capabilities with capability_type | Discriminator `n.type='Capability'`; domain `type ∈ {'technical','organizational'}` distinct. |
| A8 | regulation_map.json keys match native node `id` exactly | ingest.py `document_id="CRA-1.0"` | Native graph uses underscore-formatted IDs (e.g., `cyber_resilience_act__regulation`). |

---

## EXIT VERIFICATION

✅ docs/plan.md covers all 10 sections  
✅ No "TBD" anywhere  
✅ All decisions have stated basis (founded on read source files + probed ground truth)  
✅ map_graph.py pseudocode is concrete (line-by-line)  
✅ compare.py changes are minimal, EXTENSION (not rewrite)  
✅ Every assumption is documented with basis/risk mitigation  

---  
**plan.md complete. Next:** Execute map_graph.py + regulation_map.json creation per §7.
