# Spike: Policy System Graph → FalkorDB

Loads the Policy System domain model, as specified in
[`docs/artifacts/ps-domain-concepts.md`](../../docs/artifacts/ps-domain-concepts.md),
into FalkorDB from a plain JSON data file.

## Files

- `policy_system_graph.json` — the data: all 8 node labels (`Regulation`,
  `Role`, `Requirement`, `Obligation`, `Capability`, `Policy`, `Standard`,
  `Control`) and the 8 relationship types connecting them, populated with
  the two worked examples from the domain doc (a CRA/external chain and an
  Engineering Practices/internal chain). The two chains are built to
  converge on the same `Capability` and `Policy` nodes, so loading this file
  demonstrates the model's cross-regulation normalization, not just single
  isolated chains.
- `load_graph.py` — a generic loader. It does not hardcode per-concept
  insert functions; every node is MERGEd on `(label, id)` and every edge is
  MERGEd between the two endpoints it names, so the JSON is the single
  source of truth for what gets written.

## JSON shape

```jsonc
{
  "graph_name": "policy_system",
  "nodes": [
    { "label": "Regulation", "id": "CRA-1.0", "properties": { "...": "..." } }
  ],
  "edges": [
    {
      "type": "DEFINES",
      "from": { "label": "Regulation", "id": "CRA-1.0" },
      "to": { "label": "Role", "id": "role_manufacturer_a1b2c3" },
      "properties": { "source_ref": "Art. 13" }
    }
  ]
}
```

Edge `properties` is only non-empty where the domain doc places a fact on
the *edge* rather than a node (currently just `source_ref` on `DEFINES` and
`EXPRESSES`) — see the "fact belongs on the node or edge" principle in the
domain doc.

## Run

```bash
cd spikes/graph-ingestion3
pip install -r requirements.txt

# Start FalkorDB
podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest

# Load the data
python load_graph.py

# Re-run from a clean graph
python load_graph.py --reset
```

`load_graph.py` prints node/edge counts per type after loading, plus a
convergence check listing any `Capability` required by more than one
`Obligation` — with the shipped data that's `Security Logging`, required by
both the CRA and Engineering Practices obligations.

### Options

```
--file PATH        Path to the graph JSON file (default: policy_system_graph.json)
--host HOST         FalkorDB host (default: localhost)
--port PORT          FalkorDB port (default: 6379)
--graph-name NAME    Override the graph name (default: taken from the JSON's graph_name)
--reset              Delete the graph before loading
```
