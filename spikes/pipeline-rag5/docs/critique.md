# Critique: pipeline-rag5 Ingestion Plan

## Executive Summary

The plan contains **3 BLOCKER flaws**, **3 CRITICAL flaws**, and **4 MINOR flaws** that prevent successful implementation. The most severe issues are:

1. **Import bug (BLOCKER)**: The `_build_prune_audit_patch()` function imports `IngestionPipeline` from the wrong module path.
2. **GraphData field mismatch (BLOCKER)**: The plan uses `_document_id` which doesn't exist; `GraphData` uses `extracted_relations` and `extracted_entities`.
3. **Audit file handling (BLOCKER)**: Opening file on every write is O(n) and risks data loss.
4. **Patch scoping (CRITICAL)**: Original method references are local to factory, not in restore scope.
5. **node_counts edge type gating (CRITICAL)**: Assumes FINAL graph has `__Entity__` labels; it doesn't.
6. **FalkorDB parameters (CRITICAL)**: Plan assumes named parameters work; they don't.
7. **Attribution logic (MINOR)**: Simplistic heuristics don't perform real diff analysis.
8. **map_graph.py PBUGs (MINOR)**: PR4 fixes may not be present in copied version.
9. **ingest.py content filtering (MINOR)**: Full-corpus default behavior needs verification.
10. **SDK version guard (MINOR)**: Version attribute name needs verification.

---

## BLOCKER Flaws

### FLAW-001: Import path is incorrect

**Severity:** BLOCKER  
**Location:** `docs/plan.md:Step 1.4,_build_prune_audit_patch()`, line 104 in plan  
**What's wrong:**  
The plan specifies:
```python
from graphrag_sdk.ingestion.ingestion_pipeline import IngestionPipeline
```
This module does **not** exist. `IngestionPipeline` is exported directly from `graphrag_sdk.__init__.py`.

**FIX:**
```python
from graphrag_sdk import IngestionPipeline
```

**Impact:** Code will fail at import time with `ModuleNotFoundError`.

---

### FLAW-002: GraphData has no `_document_id` field

**Severity:** BLOCKER  
**Location:** `docs/plan.md:Step 1.4,_build_prune_audit_patch()`, `_get_run_id()` helper (lines 206-210 in plan)  
**What's wrong:**  
The plan's `_get_run_id()` and `_get_document_id()` helpers check `graph_data._document_id`:
```python
if hasattr(graph_data, '_document_id'):
    return graph_data._document_id
```
But `GraphData` object (verified via SDK source) has these fields:
- `extracted_entities: list[GraphEntity]`
- `extracted_relations: list[GraphRelationship]`

There is **no** `_document_id` field. This means `_get_run_id()` will always return `"unknown"` and audit entries will be untraceable.

**FIX:**  
The correct field is `document_id` at a higher level in the ingestion pipeline context, but `GraphData` itself does not carry a document identifier. The `document_id` string is passed to `rag.ingest()` separately and is not stored in `GraphData`.

**Option A (recommended):** Pass `document_id` through via a monkey-patch context or closure variable:
```python
# In run(), before patching:
current_document_id = doc_id  # captured from run()'s local scope

def audit_filter_quality(self, graph_data: GraphData) -> GraphData:
    ...
    run_id = current_document_id  # or generate from graph_data.nodes[0].source_ref if present
```

**Option B:** Extract a document_id from the first node's source_ref (fallback):
```python
def _get_run_id(graph_data: GraphData) -> str:
    """Extract or derive document_id from graph_data."""
    if graph_data.extracted_entities and hasattr(graph_data.extracted_entities[0], 'source_ref'):
        ref = graph_data.extracted_entities[0].source_ref
        if ref and isinstance(ref, str):
            return ref.split('/')[-1]  # crude extraction
    return "unknown"
```

**Impact:** Audit sidecar entries will have `document_id: "unknown"` for every entry, making it impossible to trace which document caused which pruning.

---

### FLAW-003: Audit file handle is reopening for every write

**Severity:** BLOCKER  
**Location:** `docs/plan.md:Step 1.4,_build_prune_audit_patch()`, `_write_audit_entry()` function (lines 235-243 in plan)  
**What's wrong:**  
The `_write_audit_entry()` function opens and closes the file on every call:
```python
def _write_audit_entry(entry: PruneAudit):
    audit_file = audit_ctx.get('audit_file')
    if audit_file:
        try:
            with open(audit_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(asdict(entry)) + "\n")
```
This is O(n) I/O overhead and risks lost entries if a write fails mid-stream.

**FIX:** Keep the file handle open in `audit_ctx` and write to it, closing only at the end:

```python
def _build_prune_audit_patch() -> tuple[callable, callable, dict]:
    ...
    # Global audit context (written by run())
    audit_ctx = {'file_handle': None, 'audit_file_path': None}
    
    def _write_audit_entry(entry: PruneAudit):
        fh = audit_ctx.get('file_handle')
        if fh:
            try:
                fh.write(json.dumps(asdict(entry)) + "\n")
                fh.flush()
            except Exception as e:
                logging.warning(f"Failed to write audit entry: {e}")
    
    return audit_filter_quality, audit_prune, audit_ctx

# In run():
# Initialize audit context with file path and opened handle
audit_ctx['audit_file_path'] = str(prune_log_path)
audit_ctx['file_handle'] = open(prune_log_path, 'a', encoding='utf-8')

# ...

# Close file at end
if audit_ctx['file_handle']:
    audit_ctx['file_handle'].close()
```

**Impact:** Performance degradation for large corpora; risk of JSONL corruption on unexpected failure.

---

## CRITICAL Flaws

### FLAW-004: Original method references are local, not returned from factory

**Severity:** CRITICAL  
**Location:** `docs/plan.md:Step 1.4,_build_prune_audit_patch()` and Step 1.8 (lines 225-229 and Step 1.8 in plan)  
**What's wrong:**  
The plan stores `_orig_filter_quality` and `_orig_prune` inside `_build_prune_audit_patch()`:
```python
# Store originals
_orig_filter_quality = IngestionPipeline._filter_quality
_orig_prune = IngestionPipeline._prune
```
But these are **local variables** to the factory. The plan's Step 1.8 tries to restore them in `run()`:
```python
IngestionPipeline._filter_quality = _orig_filter_quality
IngestionPipeline._prune = _orig_prune
```
But `_orig_filter_quality` and `_orig_prune` are **not in scope** in `run()` — they're lost when the factory returns.

**FIX:** Return the original methods from the factory so they can be restored:

```python
def _build_prune_audit_patch() -> tuple[callable, callable, dict, callable, callable]:
    ...
    # Store originals
    _orig_filter_quality = IngestionPipeline._filter_quality
    _orig_prune = IngestionPipeline._prune
    ...
    return audit_filter_quality, audit_prune, audit_ctx, _orig_filter_quality, _orig_prune

# In run():
audit_filter_quality, audit_prune, audit_ctx, orig_filter, orig_prune = _build_prune_audit_patch()

# ...

# Restore original methods
IngestionPipeline._filter_quality = orig_filter
IngestionPipeline._prune = orig_prune
```

**Impact:** The patch will fail with `UnboundLocalError: local variable '_orig_filter_quality' referenced before assignment`, or the original methods won't be restored, causing side effects in subsequent runs.

---

### FLAW-005: node_counts query assumes `__Entity__` labels on FINAL graph

**Severity:** CRITICAL  
**Location:** `docs/plan.md:Step 4, attribution.py`, `node_counts_by_label()` and `edge_counts_by_type()` functions (lines 12-16 and 19-23 in plan)  
**What's wrong:**  
The plan's attribution functions use queries like:
```python
result = graph.query(
    "MATCH (n) RETURN CASE WHEN '__Entity__' IN labels(n) THEN n.type ELSE labels(n)[0] END AS label, count(n) AS n"
)
```
This is appropriate for the **native** graph (which has `__:__Entity__` marker), but the **FINAL** graph (from `map_graph.py`) has domain labels **without** the `__Entity__` wrapper. From `map_graph.py` lines 62-75, the final graph creates nodes with direct domain labels like `(:Regulation)`, `(:Obligation)`, etc.

The `node_counts()` function in `compare.py` (lines 56-64) already handles both cases correctly with `__Entity__` gating. The attribution code must mirror that logic.

**FIX:** Use the same query as `compare.py`:

```python
def node_counts_by_label(graph) -> dict[str, int]:
    """Return counts of each domain label in graph (handles both native and final shapes)."""
    result = graph.query(
        "MATCH (n) RETURN CASE WHEN '__Entity__' IN labels(n) THEN n.type ELSE labels(n)[0] END AS label, count(n) AS n"
    )
    return {row[0]: row[1] for row in result.result_set}

def edge_counts_by_type(graph) -> dict[str, int]:
    """Return counts of each domain edge type in graph (handles both native and final shapes)."""
    result = graph.query(
        "MATCH ()-[r]->() RETURN CASE WHEN type(r) = 'RELATES' THEN r.rel_type ELSE type(r) END AS t, count(r) AS n"
    )
    return {row[0]: row[1] for row in result.result_set}
```

**Impact:** Query will return no results for the final graph, causing attribution to report 0 nodes/edges for all labels.

---

### FLAW-006: FalkorDB does not support named parameters

**Severity:** CRITICAL  
**Location:** `docs/plan.md:Step 5, content_spotcheck.py`, `spot_check_core_value_chain()` function (lines 46-50 in plan)  
**What's wrong:**  
The plan uses:
```python
baseline_result = baseline.query(baseline_query, parameters={"name": ob_name})
```
But as per critical context item #7, FalkorDB **does NOT support named parameters** (`parameters={...}`). It uses positional parameters or no parameters.

**FIX:** Use positional parameters or string interpolation:

```python
# Option A: Positional parameters (if supported)
baseline_result = baseline.query(baseline_query, ob_name)

# Option B: String interpolation (with proper escaping, as done in map_graph.py)
name_escaped = escape_cypher_string(ob_name)
baseline_query = f"""
MATCH (ob:Obligation)-[:REQUIRES]->(cap:Capability)
WHERE ob.name CONTAINS '{name_escaped}'
RETURN ob.id AS ob_id, ob.name AS ob_name, cap.id AS cap_id, cap.name AS cap_name
LIMIT 5
"""
baseline_result = baseline.query(baseline_query)
```

The `escape_cypher_string()` helper already exists in `map_graph.py` (lines 20-23); reuse it.

**Impact:** Query syntax error; no results returned.

---

## MINOR Flaws

### FLAW-007: Attribution logic is heuristic, not real diff analysis

**Severity:** MINOR  
**Location:** `docs/plan.md:Step 4, attribution.py`, classification logic (lines 108-140 in plan)  
**What's wrong:**  
The plan admits: *"The attribution logic is simplistic: it just counts audit-sidecar entries and does rough heuristics. It does NOT actually diff A-vs-B element-by-element."*

The current implementation (lines 115-125) computes:
- `β_contribution = min(β_from_audit, core_gap)` — assumes all pruned items are recoverable
- `α_contribution = core_gap - β_contribution` — assigns remaining to "extractor limit"
- `γ_contribution = governance_gap` — assumes all governance layer deficit is "ontology gap"

This is **not** a real diff. It cannot distinguish between:
- A node that existed in A, was pruned (β), but missing in B (looks like β)
- A node that never existed (α) — the audit won't have it
- A node that was pruned but B has a different extraction (real gap is not β)

**FIX:** Implement element-by-element comparison:

```python
def diff_nodes(baseline_nodes: list[dict], candidate_nodes: list[dict]) -> dict[str, list]:
    """Diff two node sets and classify each missing node."""
    baseline_set = {(n['id'], n['label']) for n in baseline_nodes}
    candidate_set = {(n['id'], n['label']) for n in candidate_nodes}
    missing = baseline_set - candidate_set
    
    classified = {"α": [], "β": [], "γ": []}
    
    for nid, label in missing:
        # Check if this node appears in prune audit
        audit_matches = [e for e in prune_audit 
                        if e.get("stage") == "prune" 
                        and e.get("id") == nid]
        
        if audit_matches:
            # Node was pruned; reason determines β vs α
            for match in audit_matches:
                if match.get("reason") in ("pattern_mismatch", "undeclared"):
                    classified["α"].append((nid, label, match["reason"]))
                else:  # dangling
                    classified["β"].append((nid, label, match["reason"]))
        else:
            # Not in audit — likely never extracted (α)
            classified["α"].append((nid, label, "not_in_audit"))
    
    return classified
```

**Rationale:** The plan's `audit_filter_quality` and `audit_prune` patches already capture the reason for each dropped node/relationship. Use that audit data to classify gaps properly, rather than relying on heuristics.

**Impact:** AC-4 verdict will be unreliable; cannot determine if SDK is genuinely missing extractions or just pruning aggressively.

---

### FLAW-008: map_graph.py adaptation assumes PBUGs are fixed

**Severity:** MINOR  
**Location:** `docs/plan.md:Step 2, map_graph.py`, "Step 2.1" (line ~90 in plan)  
**What's wrong:**  
The plan says:
> "The plan only changes 2 lines (names). But the original map-graph.py has PBUG-1, PBUG-2, PBUG-4 from pr4 — are these already fixed in the pr4 version we copied? Verify from pr4 LEARNINGS."

The current `map_graph.py` (lines 34-38) shows:
```python
# FIX PBUG-2: skip edge ONLY if an endpoint that is a Regulation has id not in keep_set
```
This suggests PBUG-2 **is** fixed. But PBUG-1 and PBUG-4 (from pr4 learnings) are not referenced.

**FIX:** Before adapting graph names, verify PBUG-1 and PBUG-4 are addressed:

1. **PBUG-1** (if known): Likely related to `map_graph.py` line 84-85 (`DELETE` vs `DELETE *` behavior)
2. **PBUG-4** (if known): Likely related to edge `MERGE` vs `MATCH/MERGE` ordering

Review `spikes/pipeline-rag4/docs/learnings.md` or the pr4 commit history to confirm. If PBUGs are present, fix them before renaming graphs.

**Impact:** The `policy_system_graphrag_final_full` graph may have incorrect structure (e.g., missing edges, dupes).

---

### FLAW-009: Full-corpus default doesn't guarantee full corpus ingestion

**Severity:** MINOR  
**Location:** `docs/plan.md:Step 1.6, build_parser()`, full-corpus defaults (lines 231-240 in plan)  
**What's wrong:**  
The plan claims:
> "Full-corpus defaults: no substantive filter, no chunk cap"

But the current `ingest.py` code (lines 180-189):
```python
if args.substantive is not None:
    pattern_str, additional_flags = _normalise_regex(args.filter_regex)
    pred = re.compile(pattern_str, additional_flags | re.I)
    chunker = FilteringChunker(chunker, pred, cap=args.substantive, spread=args.spread)
elif args.max_chunks is not None:
    chunker = CappedChunker(chunker, args.max_chunks)
```

With `--substantive None` (default) and `--max-chunks None` (default), the chunker remains `SentenceTokenCapChunking(max_tokens=512, overlap_sentences=2)` — yes, no filtering. But does this truly ingest the **full corpus**?

**Verification needed:**
- Does `SentenceTokenCapChunking` have an internal cap? (Code shows `max_tokens=512`, not a document count cap)
- Does `rag.ingest()` have a default chunk cap? (Unlikely, but verify via SDK docs)
- Does the PDF loader have a page/segment limit? (Verify in `PdfLoader` implementation)

**FIX:** Add explicit comment that the chunker will tokenize the entire document (up to memory limits):
```python
# Full-corpus mode: no FilteringChunker, no CappedChunker
# SentenceTokenCapChunking will chunk the entire document, limited only by token cap (512) per chunk
chunk_select_note = "full_document (no substantive filter)"
```

If `PdfLoader` has its own limits, add `--max-docs` to override them.

**Impact:** If PDF loader or chunker silently limits pages/chunks, the "full-corpus" claim is false.

---

### FLAW-010: SDK version guard may use wrong attribute name

**Severity:** MINOR  
**Location:** `docs/plan.md:Step 1.4,_build_prune_audit_patch()`, SDK version check (lines 82-86 in plan)  
**What's wrong:**  
The plan checks:
```python
if graphrag_sdk.__version__ != "1.4.0":
```
But Python packages don't always export `__version__` at the module level. Some use `__version__` at package level, some use `version`, some use `VERSION`.

**FIX:** Verify the correct attribute name:

```python
import graphrag_sdk

# Try multiple common patterns
version = None
for attr in ("__version__", "version", "VERSION"):
    if hasattr(graphrag_sdk, attr):
        version = getattr(graphrag_sdk, attr)
        break
if version is None:
    raise RuntimeError("graphrag_sdk does not expose a version attribute")
```

Or simply test without version guard (as the PR4 learnings may have found version guards unreliable).

**Impact:** False positive if `__version__` attribute name is wrong, blocking all runs.

---

## Recommendation

**DO NOT implement the plan as written.** Fix all BLOCKER and CRITICAL flaws before proceeding. Prioritize:

1. FLAW-001 (import path)
2. FLAW-002 (GraphData fields)
3. FLAW-003 (file handle management)
4. FLAW-004 (restore scope)
5. FLAW-005 (node counts query)
6. FLAW-006 (FalkorDB parameters)

Minor flaws (FLAW-007 to FLAW-010) should be addressed before production use but can be deferred for initial spike.

---

**Critique generated by: CRITIQUE-AGENT**  
**Timestamp:** 2026-08-17  
