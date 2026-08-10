# Graph Ingestion Tools

Reusable utilities for loading and converging Policy System graph datasets into FalkorDB.

## Scripts

- load_graph.py: Generic JSON loader for nodes and edges using label+id MERGE semantics.
- find_capability_duplicates.py: TF-IDF cosine shortlist for potential duplicate Capability nodes.
- merge_capabilities.py: Applies human-reviewed merge decisions and rewires graph edges.

## Dependencies

Install once:

```bash
pip install -r tools/graph-ingestion/requirements.txt
```

## Typical workflow

1. Load baseline and regulation datasets into one graph namespace:

```bash
python tools/graph-ingestion/load_graph.py \
  --file docs/test-data/eu-regulations/cra.json \
  --graph-name policy_system --reset

python tools/graph-ingestion/load_graph.py \
  --file docs/test-data/eu-regulations/nis2.json \
  --graph-name policy_system

python tools/graph-ingestion/load_graph.py \
  --file docs/test-data/eu-regulations/gdpr.json \
  --graph-name policy_system
```

2. Shortlist duplicate capabilities:

```bash
python tools/graph-ingestion/find_capability_duplicates.py \
  --graph-name policy_system --threshold 0.35
```

3. Record decisions in docs/test-data/eu-regulations/capability_merges.json, then apply:

```bash
python tools/graph-ingestion/merge_capabilities.py \
  --graph-name policy_system \
  --decisions docs/test-data/eu-regulations/capability_merges.json
```

Use --dry-run to preview merge effects.

## Notes

- These scripts are promoted from spikes for reusable execution.
- Spike originals remain in spikes/graph-ingestion3 as experiment history.
