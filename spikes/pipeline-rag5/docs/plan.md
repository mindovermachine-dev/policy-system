# Implementation Plan: Full-Corpus, Audited GraphRAG-SDK Extraction

**Spike Directory:** `spikes/pipeline-rag5/`  
**Target Graph Names:** `policy_system_graphrag_native_full`, `policy_system_graphrag_final_full`  
**Reference Graph:** `policy_system_cra` (FalkorDB, read-only)  
**Sidecar Output:** `logs/pruned-<ts>.jsonl` (append-only JSONL)

---

## Overview

This plan implements 5 build tasks:

1. PATCH `ingest.py` — runtime monkey-patch for audit + full-corpus default + graph name + SDK guard
2. ADAPT `map_graph.py` — change graph names to `_full` suffix versions
3. ADAPT `compare.py` — CRA-vs-CRA adaptation with ratios + remove production gates
4. α/β/γ attribution logic (AC-4)
5. Content spot-check (AC-5)

Each task has: **WHAT**, **WHERE**, **HOW**, **EXIT CRITERIA**.

---

## Task 1: PATCH `ingest.py`

### WHAT

- Add runtime monkey-patch for `_prune` and `_filter_quality` that classifies dropped nodes/rels
- Add `--prune-log` CLI flag
- Make full-corpus default (no `--substantive`, no `--max-chunks`)
- Change graph name to `policy_system_graphrag_native_full`
- Add SDK version guard (`graphrag_sdk == 1.4.0`) + `_prune` signature guard

### WHERE

**File:** `ingest.py`

**Lines to modify:**
- Import block (lines 16-31) — add `dataclasses`, `json`, `uuid`
- Module constants (lines 35-37) — change `DEFAULT_GRAPH_NAME`
- `CappedChunker` class end (~line 110) — add `PruneAudit` dataclass
- `_build_prune_audit_patch()` function (after `FilteringChunker`, ~line 150)
- `build_parser()` function — add `--prune-log`, change defaults
- `run()` function — patch registration, sidecar initialization, usage in patched functions

### HOW

#### Step 1.1: Add imports (after line 31, before `REPO_ROOT`)

```python
from dataclasses import dataclass, field, asdict
```

#### Step 1.2: Change graph name constant (line 37)

```python
DEFAULT_GRAPH_NAME = "policy_system_graphrag_native_full"
```

#### Step 1.3: Add sidecar dataclass (after line ~110, after `FilteringChunker` class)

```python
@dataclass
class PruneAudit:
    """Schema per README 'Sidecar schema'.
    
    Fields match the JSON schema in README:
    - ts, run_id, document_id, stage, kind
    - node: id, label, reason
    - rel: start, start_label, end, end_label, rel_type, source_ref, keywords, weight, chunk_ids, reason
    """
    ts: str
    run_id: str
    document_id: str
    stage: str  # "filter_quality" | "prune"
    kind: str   # "node" | "relationship"
    # node
    id: str | None = None
    label: str | None = None
    reason: str | None = None
    # relationship
    start: str | None = None
    start_label: str | None = None
    end: str | None = None
    end_label: str | None = None
    rel_type: str | None = None
    source_ref: str | None = None
    keywords: str | None = None
    weight: float | None = None
    chunk_ids: list[str] = field(default_factory=list)
```

#### Step 1.4: Add audit patch function (after line ~150, after `FilteringChunker` class)

```python
def _build_prune_audit_patch() -> tuple[callable, callable, dict]:
    """
    Monkey-patch factory for _prune/_filter_quality audit.
    
    Returns (audit_filter_quality_fn, audit_prune_fn, audit_ctx)
    where audit_ctx is a dict with:
      - 'audit_file': path to open log file
    """
    import inspect
    from graphrag_sdk import GraphData
    from graphrag_sdk.ingestion.ingestion_pipeline import IngestionPipeline

    # Guard 1: SDK version check
    try:
        import graphrag_sdk
    except ImportError:
        raise RuntimeError("graphrag_sdk must be importable for version guard")
    if graphrag_sdk.__version__ != "1.4.0":
        raise RuntimeError(
            f"graphrag_sdk version mismatch: expected 1.4.0, got {graphrag_sdk.__version__}. "
            "Refusing to patch with unknown signature."
        )

    # Guard 2: _prune signature check (per README: GraphData → GraphData)
    prune_sig = inspect.signature(IngestionPipeline._prune)
    expected_prune_params = ['self', 'graph_data', 'ontology']
    actual_prune_params = list(prune_sig.parameters.keys())
    if actual_prune_params != expected_prune_params:
        raise RuntimeError(
            f"_prune signature changed: expected {expected_prune_params}, "
            f"got {actual_prune_params}. Patch will fail; aborting."
        )

    # Guard 3: _filter_quality signature check
    try:
        filter_sig = inspect.signature(IngestionPipeline._filter_quality)
        expected_filter_params = ['self', 'graph_data']
        actual_filter_params = list(filter_sig.parameters.keys())
        if actual_filter_params != expected_filter_params:
            raise RuntimeError(
                f"_filter_quality signature changed: expected {expected_filter_params}, "
                f"got {actual_filter_params}. Patch will fail; aborting."
            )
    except AttributeError:
        raise RuntimeError(
            "_filter_quality method not found. SDK may have changed the audit stages."
        )

    # Store originals
    _orig_filter_quality = IngestionPipeline._filter_quality
    _orig_prune = IngestionPipeline._prune

    # Global audit context (written by run())
    audit_ctx = {'audit_file': None}

    def audit_filter_quality(self, graph_data: GraphData) -> GraphData:
        """Patch Step 4b: capture nodes/rels dropped by quality filter."""
        before_nodes = set(n.id for n in graph_data.nodes)
        before_rels = set((r.start_node_id, r.end_node_id, r.rel_type)
                         for r in graph_data.relationships)

        out = _orig_filter_quality(self, graph_data)

        after_nodes = set(n.id for n in out.nodes)
        after_rels = set((r.start_node_id, r.end_node_id, r.rel_type)
                        for r in out.relationships)

        dropped_nodes = before_nodes - after_nodes
        dropped_rels = before_rels - after_rels

        for nid in dropped_nodes:
            label = "Unknown"
            for n in graph_data.nodes:
                if n.id == nid:
                    label = n.label
                    break
            _write_audit_entry(PruneAudit(
                ts=datetime.now(timezone.utc).isoformat(),
                run_id=_get_run_id(graph_data),
                document_id=_get_document_id(graph_data),
                stage="filter_quality",
                kind="node",
                id=nid,
                label=label,
                reason="dangling"
            ))

        for start, end, rel_type in dropped_rels:
            _write_audit_entry(PruneAudit(
                ts=datetime.now(timezone.utc).isoformat(),
                run_id=_get_run_id(graph_data),
                document_id=_get_document_id(graph_data),
                stage="filter_quality",
                kind="relationship",
                start=start,
                end=end,
                rel_type=rel_type,
                reason="dangling",
                chunk_ids=[]
            ))

        return out

    def audit_prune(self, graph_data: GraphData, ontology) -> GraphData:
        """Patch Step 5: capture nodes/rels pruned by ontology mismatch."""
        before_nodes = set(n.id for n in graph_data.nodes)
        before_rels = set((r.start_node_id, r.end_node_id, r.rel_type)
                         for r in graph_data.relationships)

        out = _orig_prune(self, graph_data, ontology)

        after_nodes = set(n.id for n in out.nodes)
        after_rels = set((r.start_node_id, r.end_node_id, r.rel_type)
                        for r in out.relationships)

        dropped_nodes = before_nodes - after_nodes
        dropped_rels = before_rels - after_rels

        # Build mapping from node id to label
        node_labels = {n.id: n.label for n in graph_data.nodes}
        # Build mapping from relationship to (src_label, tgt_label)
        rel_labels = {}
        for r in graph_data.relationships:
            rel_labels[(r.start_node_id, r.end_node_id, r.rel_type)] = (
                node_labels.get(r.start_node_id, "Unknown"),
                node_labels.get(r.end_node_id, "Unknown")
            )

        # Determine reason for each dropped node
        declared_labels = {e.label for e in ontology.entities}
        for nid in dropped_nodes:
            label = node_labels.get(nid, "Unknown")
            if label not in declared_labels:
                reason = "label_undeclared"
            else:
                reason = "dangling"

            _write_audit_entry(PruneAudit(
                ts=datetime.now(timezone.utc).isoformat(),
                run_id=_get_run_id(graph_data),
                document_id=_get_document_id(graph_data),
                stage="prune",
                kind="node",
                id=nid,
                label=label,
                reason=reason
            ))

        # Determine reason for each dropped relationship
        declared_rel_types = {r.label for r in ontology.relations}
        declared_patterns = {tuple(p) for r in ontology.relations for p in r.patterns}
        for start, end, rel_type in dropped_rels:
            src_label, tgt_label = rel_labels.get((start, end, rel_type), ("Unknown", "Unknown"))
            
            if rel_type not in declared_rel_types:
                reason = "rel_type_undeclared"
            elif (src_label, tgt_label) not in declared_patterns:
                reason = "pattern_mismatch"
            else:
                reason = "dangling"

            _write_audit_entry(PruneAudit(
                ts=datetime.now(timezone.utc).isoformat(),
                run_id=_get_run_id(graph_data),
                document_id=_get_document_id(graph_data),
                stage="prune",
                kind="relationship",
                start=start,
                end=end,
                rel_type=rel_type,
                reason=reason,
                source_ref=_get_source_ref_from_rel(graph_data, start, end, rel_type),
                chunk_ids=_get_chunk_ids_from_rel(graph_data, start, end, rel_type)
            ))

        return out

    # Helper functions
    def _get_run_id(graph_data: GraphData) -> str:
        """Extract run_id from graph_data or generate one."""
        # GraphData has _document_id which we'll use as run_id
        if hasattr(graph_data, '_document_id'):
            return graph_data._document_id
        return "unknown"

    def _get_document_id(graph_data: GraphData) -> str:
        """Extract document_id from graph_data."""
        return _get_run_id(graph_data)

    def _get_source_ref_from_rel(graph_data: GraphData, start: str, end: str, rel_type: str) -> str | None:
        """Extract source_ref from relationship if present."""
        for r in graph_data.relationships:
            if (r.start_node_id == start and r.end_node_id == end and r.rel_type == rel_type):
                # Check if source_ref is in extra_properties
                if hasattr(r, 'extra_properties') and isinstance(r.extra_properties, dict):
                    return r.extra_properties.get('source_ref')
                # Check directly on the relationship
                if hasattr(r, 'source_ref'):
                    return r.source_ref
        return None

    def _get_chunk_ids_from_rel(graph_data: GraphData, start: str, end: str, rel_type: str) -> list[str]:
        """Extract chunk_ids from relationship if present."""
        for r in graph_data.relationships:
            if (r.start_node_id == start and r.end_node_id == end and r.rel_type == rel_type):
                if hasattr(r, 'extra_properties') and isinstance(r.extra_properties, dict):
                    chunk_ids = r.extra_properties.get('chunk_ids')
                    if isinstance(chunk_ids, list):
                        return chunk_ids
                return []
        return []

    def _write_audit_entry(entry: PruneAudit):
        """Write to sidecar file if audit context is initialized."""
        audit_file = audit_ctx.get('audit_file')
        if audit_file:
            try:
                with open(audit_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(asdict(entry)) + "\n")
            except Exception as e:
                logging.warning(f"Failed to write audit entry: {e}")

    return audit_filter_quality, audit_prune, audit_ctx
```

#### Step 1.5: Add helper function to get graph_data's document_id

Add this helper after `_write_audit_entry`:

```python
def _get_run_id(graph_data: GraphData) -> str:
    """Extract run_id from graph_data or generate one."""
    if hasattr(graph_data, '_document_id'):
        return graph_data._document_id
    return "unknown"
```

#### Step 1.6: Update `build_parser()` (lines ~105-130)

Change defaults and add `--prune-log`:

```python
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ingest CRA into FalkorDB GraphRAG (spike)")
    p.add_argument("--source", choices=["cra"], required=False, default="cra",
                   help="Source to ingest (default: cra)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=6379)
    p.add_argument("--graph-name", default=DEFAULT_GRAPH_NAME)
    p.add_argument("--max-concurrency", type=int, default=2,
                   help="Concurrency for the shared LLM+extraction gate (default 2)")
    p.add_argument("--reset", action="store_true",
                   help="Delete the TARGET graph before ingest (scoped to --graph-name)")
    # Full-corpus defaults: no substantive filter, no chunk cap
    p.add_argument("--max-chunks", type=int, default=None,
                   help="[DEPRECATED] Full-corpus mode: max chunks now unrestricted. "
                        "This flag has no effect.")
    p.add_argument("--substantive", type=int, default=None,
                   help="[DEPRECATED] Full-corpus mode: no content filtering by default.")
    p.add_argument("--filter-regex", default="shall|should",
                   help="Deprecated: no filtering applied in full-corpus mode.")
    p.add_argument("--spread", action="store_true",
                   help="Deprecated: no stratification in full-corpus mode.")
    p.add_argument("--prune-log", type=str, default=None,
                   help="Path to JSONL sidecar for prune/quality audit (default: logs/pruned-<ts>.jsonl)")
    return p
```

#### Step 1.7: Patch registration in `run()` (after line ~250, after `llm = RateLimitedLLM(...)`)

```python
async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(...)
    _require_azure_env()

    # === TASK 1: SDK version guard + monkey-patch registration ===
    audit_filter_quality, audit_prune, audit_ctx = _build_prune_audit_patch()

    # Determine prune log path
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if args.prune_log:
        prune_log_path = Path(args.prune_log)
    else:
        prune_log_path = LOG_DIR / f"pruned-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.jsonl"
    
    # Initialize audit context with file path
    audit_ctx['audit_file'] = str(prune_log_path)
    
    # Monkey-patch IngestionPipeline
    from graphrag_sdk.ingestion.ingestion_pipeline import IngestionPipeline
    IngestionPipeline._filter_quality = audit_filter_quality
    IngestionPipeline._prune = audit_prune
```

#### Step 1.8: Close audit file at end of `run()` (after line ~350, before `print("Done...")`)

```python
    log_fh.close()

    # Close sidecar audit log
    audit_ctx['audit_file'] = None  # Stop writes

    # Restore original methods
    IngestionPipeline._filter_quality = _orig_filter_quality
    IngestionPipeline._prune = _orig_prune

    print(f"Done. graph={args.graph_name} sources={keys} errors={len(errors)} "
          f"total_s={round(time.perf_counter() - t0, 2)} -> {log_file}")
    return 1 if errors else 0
```

**Important:** You must also save the original methods before monkey-patching. Add at the top of the monkey-patch registration:

```python
    # Store originals before patching
    _orig_filter_quality = IngestionPipeline._filter_quality
    _orig_prune = IngestionPipeline._prune
    
    # Monkey-patch IngestionPipeline
    IngestionPipeline._filter_quality = audit_filter_quality
    IngestionPipeline._prune = audit_prune
```

And restore after use:

```python
    # Restore original methods
    IngestionPipeline._filter_quality = _orig_filter_quality
    IngestionPipeline._prune = _orig_prune
```

### EXIT CRITERIA

1. `policy_system_graphrag_native_full` graph is populated with no ingest errors
2. `logs/pruned-<timestamp>.jsonl` file exists with classified entries
3. Each entry follows the sidecar schema:
   - `ts`, `run_id`, `document_id`, `stage`, `kind`
   - For nodes: `id`, `label`, `reason`
   - For relationships: `start`, `start_label`, `end`, `end_label`, `rel_type`, `source_ref`, `chunk_ids`, `reason`
4. SDK version guard raises RuntimeError if version ≠ 1.4.0
5. Signature guard raises RuntimeError if `_prune` signature changes

---

## Task 2: ADAPT `map_graph.py`

### WHAT

- Change `native_name` to `policy_system_graphrag_native_full`
- Change `final_name` to `policy_system_graphrag_final_full`

### WHERE

**File:** `map_graph.py`

**Lines to modify:**
- Line 90-95 in `map_graph()` function call at end of file
- Optional: CLI defaults in `__main__` guard

### HOW

#### Step 2.1: Update function call (line ~90)

```python
if __name__ == "__main__":
    map_graph(
        native_name="policy_system_graphrag_native_full",  # Changed from _native
        final_name="policy_system_graphrag_final_full",    # Changed from _final
        regulation_map_path="regulation_map.json",
    )
```

### EXIT CRITERIA

1. `map_graph.py` uses `policy_system_graphrag_native_full` as source
2. `map_graph.py` outputs to `policy_system_graphrag_final_full`
3. All other functionality remains unchanged

---

## Task 3: ADAPT `compare.py`

### WHAT

- Compare `policy_system_graphrag_final_full` vs `policy_system_cra`
- Per-label and per-edge-type counts + RATIOS (A-vs-B)
- DEFECT-1 check (Capability type collision = 0)
- Unknown labels = 0
- `capability_convergence()` runs without error
- REMOVE production cross-ref gate (`cross_ref_reg_percentage`)
- No `__Entity__/RELATES` leakage check

### WHERE

**File:** `compare.py`

**Lines to modify:**
- `report()` function (lines ~80-180) — adapt graph comparisons, add ratios, remove cross-ref gate
- Line ~200 in `main()` — change defaults

### HOW

#### Step 3.1: Change defaults in `main()` (line ~200)

```python
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--baseline-graph", default="policy_system_cra", help="Reference graph (LLM-on-cra.md)")
    parser.add_argument("--graphrag-graph", default="policy_system_graphrag_final_full", help="This spike's graph (map_graph.py)")
    parser.add_argument("--keep-set-path", default="regulation_map.json", help="Path to regulation_map.json (keys = native Regulation IDs to keep)")
    args = parser.parse_args()
```

#### Step 3.2: Update `report()` function to add ratio reporting

Replace the `report()` function (lines ~60-210) with:

```python
def report(db: FalkorDB, graph_name: str, label: str, args, is_baseline: bool = False) -> None:
    """Report node/edge counts and ratios (A vs B)."""
    print(f"\n=== {label}: graph '{graph_name}' ===")
    graph = db.select_graph(graph_name)

    nodes = node_counts(graph)
    edges = edge_counts(graph)

    print("Node counts:")
    for node_label in sorted(EXPECTED_LABELS):
        count = nodes.get(node_label, 0)
        print(f"  {node_label:14s} {count}")
    unexpected_nodes = set(nodes) - EXPECTED_LABELS
    if unexpected_nodes:
        print(f"  UNEXPECTED LABELS (not in ps-domain-concepts.md): {sorted(unexpected_nodes)}")

    print("Edge counts:")
    for edge_type in sorted(EXPECTED_EDGE_TYPES):
        count = edges.get(edge_type, 0)
        print(f"  {edge_type:14s} {count}")
    unexpected_edges = set(edges) - EXPECTED_EDGE_TYPES
    if unexpected_edges:
        print(f"  UNEXPECTED EDGE TYPES (not in ps-domain-concepts.md): {sorted(unexpected_edges)}")

    # === ORCH-D10: Governance-absent flag (expected for CRA-only) ===
    governance_labels = ["Policy", "Standard", "Control", "RiskPath"]
    governance_absent = all(nodes.get(lbl, 0) == 0 for lbl in governance_labels)
    gov_present = {lbl: nodes.get(lbl, 0) for lbl in governance_labels if nodes.get(lbl, 0) > 0}
    if governance_absent:
        print(f"  ⚠ GOVERNANCE LAYER: Policy/Standard/Control/RiskPath = 0")
    else:
        print(f"  ⚠ GOVERNANCE LAYER: present={gov_present}")

    # === REMOVE production cross-ref gate (per README: meaningless for CRA-vs-CRA) ===
    # cross_ref_reg_percentage() removed entirely — no cross-ref gate for CRA baseline

    converged = capability_convergence(graph)
    print(f"Capabilities converged across >1 Regulation: {len(converged)}")
    for name, n in converged:
        print(f"  {name!r} <- {n} regulations")

    # === DEFECT-1, unknown labels, convergence checks (AC-3) ===
    unknown_labels = set(nodes) - EXPECTED_LABELS
    unknown_ok = len(unknown_labels) == 0

    # DEFECT-1 check: Capability nodes where type=="Capability" or null
    res_def1 = graph.query("MATCH (n:Capability) WHERE n.type = 'Capability' OR n.type IS NULL RETURN count(*) AS c")
    defect1 = res_def1.result_set[0][0]
    defect1_ok = defect1 == 0

    # Convergence check
    conv_ok = converged is not None  # Just check it ran without error

    print(f"Unknown labels: {sorted(unknown_labels)} {'OK' if unknown_ok else 'FAIL'}")
    print(f"DEFECT-1 (Capability type collision): {defect1} {'OK' if defect1_ok else 'FAIL'}")

    # === AC-3: Ratio reporting (A vs B) only for candidate graph ===
    if not is_baseline:
        # Get baseline counts (assuming baseline is policy_system_cra)
        baseline_name = args.baseline_graph
        baseline_graph = db.select_graph(baseline_name)
        baseline_nodes = node_counts(baseline_graph)
        baseline_edges = edge_counts(baseline_graph)

        print("\n=== RATIOS (Candidate / Baseline) ===")
        print("Node ratios (Candidate / Baseline):")
        for node_label in sorted(EXPECTED_LABELS):
            baseline_count = baseline_nodes.get(node_label, 0)
            cand_count = nodes.get(node_label, 0)
            if baseline_count > 0:
                ratio = cand_count / baseline_count
                print(f"  {node_label:14s} {cand_count:5d} / {baseline_count:5d} = {ratio:.2f}")
            else:
                ratio = float('inf') if cand_count > 0 else 1.0
                print(f"  {node_label:14s} {cand_count:5d} / {baseline_count:5d} = N/A")

        print("Edge ratios (Candidate / Baseline):")
        for edge_type in sorted(EXPECTED_EDGE_TYPES):
            baseline_count = baseline_edges.get(edge_type, 0)
            cand_count = edges.get(edge_type, 0)
            if baseline_count > 0:
                ratio = cand_count / baseline_count
                print(f"  {edge_type:14s} {cand_count:5d} / {baseline_count:5d} = {ratio:.2f}")
            else:
                ratio = float('inf') if cand_count > 0 else 1.0
                print(f"  {edge_type:14s} {cand_count:5d} / {baseline_count:5d} = N/A")

    # === FINAL VERDICT (AC-3) ===
    print("\n=== FINAL VERDICT ===")
    
    total_domain = sum(nodes.get(lbl, 0) for lbl in EXPECTED_LABELS)
    print(f"Domain entities: {total_domain} (expected >=80) {'PASS' if total_domain >= 80 else 'FAIL'}")
    
    core_edges = {"DEFINES": edges.get("DEFINES", 0), "HAS": edges.get("HAS", 0),
                     "REQUIRES": edges.get("REQUIRES", 0), "SATISFIED_BY": edges.get("SATISFIED_BY", 0)}
    core_ok = all(v > 0 for v in core_edges.values())
    print(f"Core chain edges (DEFINES={core_edges['DEFINES']}, HAS={core_edges['HAS']}, REQUIRES={core_edges['REQUIRES']}, SATISFIED_BY={core_edges['SATISFIED_BY']}) {'PASS' if core_ok else 'FAIL'}")
    
    expr = edges.get("EXPRESSES", 0)
    print(f"  EXPRESSES={expr} (REPORTED, not graded: 0 expected)")

    print(f"Unknown labels: {sorted(unknown_labels)} {'PASS' if unknown_ok else 'FAIL'}")
    print(f"DEFECT-1: {defect1} {'PASS' if defect1_ok else 'FAIL'}")
    print(f"cap_convergence ran: {'PASS' if conv_ok else 'FAIL'}")

    # No cross-ref % gate (AC-3: removed for CRA-vs-CRA)
    all_pass = total_domain >= 80 and core_ok and unknown_ok and defect1_ok and conv_ok
    print(f"\nFINAL: {'PASS' if all_pass else 'FAIL'}")
```

#### Step 3.3: Update `report()` calls in `main()` (line ~210)

```python
db = FalkorDB(host=args.host, port=args.port)

report(db, args.baseline_graph, "BASELINE (policy_system_cra)", args, is_baseline=True)
report(db, args.graphrag_graph, "GRAPHRAG-SDK (this spike)", args, is_baseline=False)
```

### EXIT CRITERIA

1. `compare.py` runs on `policy_system_graphrag_final_full` vs `policy_system_cra`
2. Per-label and per-edge-type counts + ratios printed
3. DEFECT-1 check (Capability type collision = 0) runs without error
4. Unknown labels = 0 check runs without error
5. `capability_convergence()` runs without error
6. Production cross-ref gate (`cross_ref_reg_percentage`) is removed
7. No `__Entity__/RELATES` leakage check (removed per README)

---

## Task 4: α/β/γ ATTRIBUTION LOGIC (AC-4)

### WHAT

- Using pruned audit sidecar + raw extraction diff
- Classify B's gap vs A into: β (pruned/sampled, recoverable), α (never extracted), γ (SDK cannot model)
- Emit one-paragraph verdict: 'SDK is/is not a better extractor, and why'

### WHERE

**File:** `spikes/pipeline-rag5/` — new file `attribution.py`

### HOW

#### Step 4.1: Create `attribution.py`

**File:** `attribution.py`

```python
#!/usr/bin/env python3
"""
AC-4: α/β/γ Attribution Logic

Classify the gap between graph B (GraphRAG-SDK, full-corpus) and graph A (policy_system_cra)
into three categories:

- α: Never extracted even when present in source (extractor limit)
- β: Pruned/sampled but recoverable (sampling/filtering confound)
- γ: SDK cannot model this layer by design (ontology gap)

Output: attribution.jsonl + one-paragraph verdict
"""

import json
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from falkordb import FalkorDB


REPO_ROOT = Path(__file__).resolve().parents[2]
SPIKE_DIR = Path(__file__).resolve().parent
LOG_DIR = SPIKE_DIR / "logs"
CRA_SOURCE = REPO_ROOT / "docs/regulations/CRA.pdf"


def load_prune_audit() -> list[dict]:
    """Load the most recent pruned-audit JSONL from logs/."""
    prune_files = sorted(LOG_DIR.glob("pruned-*.jsonl"), reverse=True)
    if not prune_files:
        raise FileNotFoundError("No prune-audit log found in logs/")
    
    entries = []
    with open(prune_files[0]) as f:
        for line in f:
            entries.append(json.loads(line.strip()))
    return entries


def node_counts_by_label(graph) -> dict[str, int]:
    """Return counts of each domain label in graph."""
    result = graph.query(
        "MATCH (n) RETURN CASE WHEN '__Entity__' IN labels(n) THEN n.type ELSE labels(n)[0] END AS label, count(n) AS n"
    )
    return {row[0]: row[1] for row in result.result_set}


def edge_counts_by_type(graph) -> dict[str, int]:
    """Return counts of each domain edge type in graph."""
    result = graph.query(
        "MATCH ()-[r]->() RETURN CASE WHEN type(r) = 'RELATES' THEN r.rel_type ELSE type(r) END AS t, count(r) AS n"
    )
    return {row[0]: row[1] for row in result.result_set}


def extract_pruned_reason_counts(prune_audit: list[dict]) -> dict[str, int]:
    """Extract counts of each prune reason from audit log."""
    reason_counts = defaultdict(int)
    for entry in prune_audit:
        reason = entry.get("reason", "unknown")
        stage = entry.get("stage", "unknown")
        kind = entry.get("kind", "unknown")
        reason_counts[f"{stage}:{reason}:{kind}"] += 1
    return dict(reason_counts)


def run_attribution(baseline_graph_name: str = "policy_system_cra",
                   candidate_graph_name: str = "policy_system_graphrag_final_full") -> None:
    """
    Run α/β/γ attribution analysis and write results to logs/attribution-*.jsonl.
    """
    from datetime import timezone
    
    db = FalkorDB(host="localhost", port=6379)
    
    # Load graphs
    baseline = db.select_graph(baseline_graph_name)
    candidate = db.select_graph(candidate_graph_name)
    
    # Load prune audit
    try:
        prune_audit = load_prune_audit()
    except FileNotFoundError:
        print("⚠ No prune-audit log found; attribution will be incomplete.")
        prune_audit = []
    
    # Get counts
    baseline_nodes = node_counts_by_label(baseline)
    candidate_nodes = node_counts_by_label(candidate)
    baseline_edges = edge_counts_by_type(baseline)
    candidate_edges = edge_counts_by_type(candidate)
    
    # Compute gaps
    node_gaps = {}
    for label in baseline_nodes:
        diff = baseline_nodes[label] - candidate_nodes.get(label, 0)
        if diff > 0:
            node_gaps[label] = diff
    
    edge_gaps = {}
    for etype in baseline_edges:
        diff = baseline_edges[etype] - candidate_edges.get(etype, 0)
        if diff > 0:
            edge_gaps[etype] = diff
    
    # Classify gaps
    audit_reasons = extract_pruned_reason_counts(prune_audit)
    
    # Heuristic: if β reasons are high, gap is likely β; otherwise α
    # If gap集中在 governance layer (Policy/Standard/Control/RiskPath), it's likely γ
    governance_labels = {"Policy", "Standard", "Control", "RiskPath"}
    governance_gap = sum(node_gaps.get(lbl, 0) for lbl in governance_labels)
    core_gap = sum(v for k, v in node_gaps.items() if k not in governance_labels)
    
    if core_gap == 0 and governance_gap > 0:
        γ_contribution = governance_gap
        α_contribution = 0
        β_contribution = 0
    else:
        # Rough heuristic: assign β to audit-driven prunes, rest to α
        # Count β reasons from audit (filter_quality:dangling, prune:pattern_mismatch, etc.)
        β_from_audit = sum(
            count for reason, count in audit_reasons.items()
            if "dangling" in reason or "pattern_mismatch" in reason or "undeclared" in reason
        )
        β_contribution = min(β_from_audit, core_gap)  # Can't be more than core gap
        α_contribution = core_gap - β_contribution
        γ_contribution = governance_gap
    
    # Emit verdict
    verdict = ""
    if β_contribution > α_contribution and β_contribution > γ_contribution:
        verdict = (
            "The GraphRAG-SDK is a viable extractor for the policy_graph schema. "
            "Most of the gap vs the reference graph consists of nodes/relationships that were "
            "pruned or filtered in the SDK pipeline (β), not absent from the source (α). "
            "Adjusting the SDK's sampling or pruning settings should close the gap."
        )
    elif α_contribution > β_contribution and α_contribution > γ_contribution:
        verdict = (
            "The GraphRAG-SDK is not a better extractor than the reference graph. "
            "The gap is dominated by α — facts that were present in the source but "
            "never extracted by the SDK's LLM pipeline. The extractor's limitation, "
            "not the pipeline's filtering, is the bottleneck."
        )
    elif γ_contribution > 0:
        verdict = (
            "The GraphRAG-SDK is not a drop-in replacement for the policy_graph schema "
            "because it cannot model the governance layer (Policy/Standard/Control/RiskPath) "
            "by design (γ). The deficit is structural, not a matter of better extraction or "
            "different sampling. Use the SDK for the obligation/capability core, but "
            "supplement it with manual mapping for the governance layer."
        )
    else:
        verdict = (
            "The GraphRAG-SDK matches the reference graph within expected variance. "
            "No single attribution factor dominates; the gap is distributed across "
            "β (pruned/sampled) and α (extractor limit) factors."
        )
    
    # Write results
    attribution = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "baseline_graph": baseline_graph_name,
        "candidate_graph": candidate_graph_name,
        "node_gaps": node_gaps,
        "edge_gaps": edge_gaps,
        "audit_pruned_reasons": audit_reasons,
        "attribution_estimate": {
            "α": α_contribution,
            "β": β_contribution,
            "γ": γ_contribution,
            "total_gap": sum(node_gaps.values()) + sum(edge_gaps.values())
        },
        "verdict": verdict
    }
    
    log_path = LOG_DIR / f"attribution-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps(attribution) + "\n")
    
    print(f"\n=== α/β/γ Attribution ===")
    print(f"Node gaps: {node_gaps}")
    print(f"Edge gaps: {edge_gaps}")
    print(f"Audit-pruned counts: {audit_reasons}")
    print(f"Attribution (estimated):")
    print(f"  α (extractor limit): {α_contribution}")
    print(f"  β (pruned/sampled):  {β_contribution}")
    print(f"  γ (ontology gap):    {γ_contribution}")
    print(f"\nVerdict:")
    print(verdict)
    print(f"\nFull results written to {log_path}")


if __name__ == "__main__":
    run_attribution()
```

### EXIT CRITERIA

1. `attribution.py` runs without error
2. `logs/attribution-<ts>.jsonl` is created with complete analysis
3. Verdict paragraph is generated with clear classification into α/β/γ
4. Attribution estimates include counts for each category

---

## Task 5: CONTENT SPOT-CHECK (AC-5)

### WHAT

- Query `policy_system_cra` for core value chain nodes/edges
- Compare against `policy_system_graphrag_final_full` for a few obligations/capabilities
- Document qualitatively in `docs/content-spotcheck.md`

### WHERE

**File:** `spikes/pipeline-rag5/` — new file `content_spotcheck.py`

### HOW

#### Step 5.1: Create `content_spotcheck.py`

**File:** `content_spotcheck.py`

```python
#!/usr/bin/env python3
"""
AC-5: Content Spot-Check

Manual qualitative comparison of core value chain (Def → Requires → SatisfiedBy → …)
between policy_system_cra (reference) and policy_system_graphrag_final_full (candidate).
"""

from pathlib import Path
from falkordb import FalkorDB


def spot_check_core_value_chain(
    baseline_graph_name: str = "policy_system_cra",
    candidate_graph_name: str = "policy_system_graphrag_final_full"
) -> None:
    """
    Spot-check core value chain between baseline and candidate graphs.
    """
    db = FalkorDB(host="localhost", port=6379)
    
    baseline = db.select_graph(baseline_graph_name)
    candidate = db.select_graph(candidate_graph_name)
    
    # Spot-check items: pick a few Obligations/Capabilities known to exist in baseline
    spot_check_items = [
        {"label": "Obligation", "search": "Risk Assessment"},
        {"label": "Obligation", "search": "Vulnerability Handling"},
        {"label": "Obligation", "search": "Security Updating"},
    ]
    
    results = {"baseline": {}, "candidate": {}}
    
    for item in spot_check_items:
        ob_name = item["search"]
        
        # Query baseline
        baseline_query = """
        MATCH (ob:Obligation)-[:REQUIRES]->(cap:Capability)
        WHERE ob.name CONTAINS $name
        RETURN ob.id AS ob_id,
               ob.name AS ob_name,
               cap.id AS cap_id,
               cap.name AS cap_name
        LIMIT 5
        """
        baseline_result = baseline.query(baseline_query, parameters={"name": ob_name})
        
        baseline_matches = []
        for row in baseline_result.result_set:
            baseline_matches.append({
                "ob_id": row[0],
                "ob_name": row[1],
                "cap_id": row[2],
                "cap_name": row[3],
            })
        
        results["baseline"][ob_name] = baseline_matches
        
        # Query candidate
        candidate_result = candidate.query(baseline_query, parameters={"name": ob_name})
        
        candidate_matches = []
        for row in candidate_result.result_set:
            candidate_matches.append({
                "ob_id": row[0],
                "ob_name": row[1],
                "cap_id": row[2],
                "cap_name": row[3],
            })
        
        results["candidate"][ob_name] = candidate_matches
    
    # Write results
    docs_dir = Path(__file__).resolve().parents[0] / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    
    spotcheck_path = docs_dir / "content-spotcheck.md"
    
    with open(spotcheck_path, "w") as f:
        f.write("# Content Spot-Check Report\n\n")
        f.write("## Methodology\n\n")
        f.write("Compare core value chain between `policy_system_cra` (reference) and "
                "`policy_system_graphrag_final_full` (candidate) for a few obligation/capability pairs.\n\n")
        
        f.write("## Results\n\n")
        for ob_name, baseline_matches in results["baseline"].items():
            candidate_matches = results["candidate"].get(ob_name, [])
            
            f.write(f"### {ob_name}\n\n")
            f.write(f"**Baseline ({len(baseline_matches)} matches):**\n\n")
            if baseline_matches:
                for m in baseline_matches:
                    f.write(f"- {m['ob_name']} → {m['cap_name']} (ob_id={m['ob_id']}, cap_id={m['cap_id']})\n")
            else:
                f.write("- (no matches)\n")
            
            f.write(f"\n**Candidate ({len(candidate_matches)} matches):**\n\n")
            if candidate_matches:
                for m in candidate_matches:
                    f.write(f"- {m['ob_name']} → {m['cap_name']} (ob_id={m['ob_id']}, cap_id={m['cap_id']})\n")
            else:
                f.write("- (no matches)\n")
            
            if len(baseline_matches) != len(candidate_matches):
                f.write(f"\n**DISCREPANCY:** Baseline has {len(baseline_matches)} matches, candidate has {len(candidate_matches)}\n")
            elif baseline_matches and candidate_matches:
                # Verify IDs match
                baseline_ids = {(m['ob_id'], m['cap_id']) for m in baseline_matches}
                candidate_ids = {(m['ob_id'], m['cap_id']) for m in candidate_matches}
                if baseline_ids == candidate_ids:
                    f.write("\n**MATCH:** Same Obligation→Capability pairs in both graphs.\n")
                else:
                    f.write(f"\n**MISMATCH:** Different Obligation→Capability pairs found.\n")
                    f.write(f"  Baseline: {baseline_ids}\n")
                    f.write(f"  Candidate: {candidate_ids}\n")
        
        f.write("\n## Observations\n\n")
        f.write("- TODO: Add qualitative analysis of quality, completeness, and fidelity.\n")
    
    print(f"Spot-check report written to {spotcheck_path}")


if __name__ == "__main__":
    spot_check_core_value_chain()
```

### EXIT CRITERIA

1. `content_spotcheck.py` runs without error
2. `docs/content-spotcheck.md` is created with manual spot-check results
3. Core value chain (Obligation→Capability) is compared for a few key items
4. File includes:
   - Methodology section
   - Results section with counts and IDs
   - Discrepancy detection
   - Observations section for qualitative analysis

---

## Final Build Order

1. **Copy** all source files from `pipeline-rag3`/`pipeline-rag4`:
   - `ingest.py` (patched with new functionality)
   - `schema.py` (verbatim)
   - `regulation_map.json` (verbatim)
   - `map_graph.py` (adapted with new graph names)
   - `compare.py` (adapted for CRA-vs-CRA)
2. **Run** `ingest.py --source cra --reset` to create `policy_system_graphrag_native_full`
3. **Run** `map_graph.py` to create `policy_system_graphrag_final_full`
4. **Run** `compare.py` to generate structural comparison report
5. **Run** `attribution.py` to generate α/β/γ verdict
6. **Run** `content_spotcheck.py` to generate spot-check report

---

## Verification Checklist

- [ ] `logs/pruned-*.jsonl` exists with classified entries per README schema
- [ ] `logs/attribution-*.jsonl` exists with α/β/γ attribution
- [ ] `docs/content-spotcheck.md` exists with qualitative analysis
- [ ] `compare.py` output shows per-label/edge ratios
- [ ] DEFECT-1 check passed (0 capability type collisions)
- [ ] Unknown labels = 0
- [ ] `capability_convergence()` runs without error
- [ ] No production cross-ref gate applied
- [ ] α/β/γ verdict paragraph is generated

---

**Plan version:** 1.0.0  
**Last updated:** 2026-08-17
