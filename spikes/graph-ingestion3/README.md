# Spike: Policy System Graph → FalkorDB

Loads the Policy System domain model, as specified in
[`docs/artifacts/ps-domain-concepts.md`](../../docs/artifacts/ps-domain-concepts.md),
into FalkorDB from plain JSON data files — one per regulation — and proves
out that multiple regulations can converge on shared `Capability` nodes in
a single graph.

Promoted copies of reusable extracted datasets now live in
[`docs/test-data/eu-regulations`](../../docs/test-data/eu-regulations).

Promoted reusable loader/convergence scripts now live in
[`tools/graph-ingestion`](../../tools/graph-ingestion).

## Files

- `policy_system_graph.json` — the original worked example: all 8 node
  labels (`Regulation`, `Role`, `Requirement`, `Obligation`, `Capability`,
  `Policy`, `Standard`, `Control`) and the 8 relationship types connecting
  them, populated with two hand-built chains (a CRA/external chain and an
  Engineering Practices/internal chain) built to converge on the same
  `Capability` and `Policy` nodes.
- `cra.json` — a full LLM extraction of CRA (EU 2024/2847) from
  `docs/regulations/CRA.md`: Roles, Requirements, Obligations and
  Capabilities only (no Policy/Standard/Control layer yet). See
  `cra-prompt.md` (the extraction prompt) and
  `cra-extraction-methodology.md` (the judgment calls — role/requirement/
  obligation/capability boundaries, granularity, what got excluded and why)
  for how it was derived.
- `nis2.json` — the same extraction pattern applied to NIS2 (EU 2022/2555)
  from `docs/regulations/NIS2.md`, scoped to Art. 3(1)-(4), 20, 21 and
  23 (see `nis2-prompt.md`). `nis2-extraction-methodology.md` records the
  judgment calls, including where it diverges from CRA's (Member-State
  transposition wrapper stripped from every duty; Capability convergence
  onto CRA's ids applied directly at extraction time rather than left for
  the duplicate-finder). GDPR extraction is expected to follow the same
  pattern, with its own `gdpr-prompt.md` / `gdpr-extraction-methodology.md`.
- `load_graph.py` — a generic loader. It does not hardcode per-concept
  insert functions; every node is MERGEd on `(label, id)` and every edge is
  MERGEd between the two endpoints it names, so the JSON is the single
  source of truth for what gets written. Never resets the graph unless
  `--reset` is passed, so loading a second regulation's file into the same
  `--graph-name` layers it on top of what's already there rather than
  replacing it.
- `find_capability_duplicates.py` / `merge_capabilities.py` /
  `capability_merges.json` — the cross-regulation Capability convergence
  workflow. See [Loading multiple regulations](#loading-multiple-regulations-into-one-graph)
  below.

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

## Run (single regulation)

```bash
cd ../..
pip install -r tools/graph-ingestion/requirements.txt

# Start FalkorDB
podman run --rm -d --name falkordb -p 6379:6379 falkordb/falkordb:latest

# Load the data
python tools/graph-ingestion/load_graph.py --file docs/test-data/eu-regulations/cra.json --graph-name policy_system

# Re-run from a clean graph
python tools/graph-ingestion/load_graph.py --file docs/test-data/eu-regulations/cra.json --graph-name policy_system --reset
```

`load_graph.py` prints node/edge counts per type after loading, plus a
convergence check listing any `Capability` required by more than one
`Obligation`.

### `load_graph.py` options

```
--file PATH        Path to the graph JSON file (default: policy_system_graph.json)
--host HOST         FalkorDB host (default: localhost)
--port PORT          FalkorDB port (default: 6379)
--graph-name NAME    Override the graph name (default: taken from the JSON's graph_name)
--reset              Delete the graph before loading
```

Note: a JSON file's own `graph_name` field reflects whatever the extraction
happened to set it to (`cra.json`'s is `policy_system`, which happens to be
correct — see below — but a future NIS2/GDPR extraction has no reason to
get that right on its own). Always pass `--graph-name policy_system`
explicitly rather than relying on a given file's default.

## Loading multiple regulations into one graph

Goal: extract NIS2, GDPR, etc. the same way `cra.json` was extracted, and
load each into the *same* graph namespace to prove the domain model
converges across regulations rather than staying siloed per-file.

That namespace is `policy_system` — named for the domain model the graph
represents (`docs/artifacts/ps-domain-concepts.md`), not for whichever
regulation happens to load first. The graph also isn't scoped to
regulations specifically: `policy_system_graph.json`'s Engineering
Practices chain is an *internal* policy, not a regulation, converging on
the same Capability/Policy nodes — `policy_system` is the name that still
makes sense once that layer is loaded too. Don't reach for a
regulation-named graph (`cra_v1`, `nis2_v1`, ...) even for a quick test;
regulation identity and version already live on the `Regulation` node's own
id (e.g. `EU-2024/2847-v1`).

```bash
python tools/graph-ingestion/load_graph.py --file docs/test-data/eu-regulations/cra.json  --graph-name policy_system   # base load
python tools/graph-ingestion/load_graph.py --file docs/test-data/eu-regulations/nis2.json --graph-name policy_system   # layers on top, no --reset
python tools/graph-ingestion/load_graph.py --file docs/test-data/eu-regulations/gdpr.json --graph-name policy_system   # layers on top, no --reset
```

Because `load_graph.py` never resets unless told to, and every node/edge is
MERGEd rather than inserted, this just works for `Role`, `Requirement` and
`Obligation` — their ids are naturally regulation-scoped (e.g.
`CRA-1.0_req_art_13.1`), so two regulations can never accidentally collide
on identity there.

`Capability` is the one label that's *supposed* to converge across
regulations (that's the point of the domain model — see
`docs/artifacts/ps-domain-concepts.md`), but each regulation is extracted
independently, so the same underlying capacity (e.g. "Vulnerability
Management") can come out of NIS2's extraction with a different id and
different phrasing than CRA's. Nothing in `load_graph.py` will merge those
automatically, and it shouldn't try to — collapsing two capabilities is a
domain judgment call, not something a hash or a string match should decide
silently. That's what the workflow below is for.

## Capability convergence workflow

After loading a new regulation into a shared graph, run the duplicate
finder to shortlist candidate matches against everything already in the
graph:

```bash
python tools/graph-ingestion/find_capability_duplicates.py --graph-name policy_system
```

It scores every pair of `Capability` nodes with TF-IDF cosine similarity
over their name + description (corpus-relative — words nearly every
Capability shares, like "technical capacity", get automatically
down-weighted rather than relying on a hand-tuned stopword list) and prints
ranked pairs above `--threshold` (default `0.35`), each with its source
regulation(s) for context. Results are flat pairs, not transitively-merged
clusters — clustering here would chain unrelated capabilities together
through weak intermediate matches (this was tried and produced false
groupings on nothing but shared generic words).

Review the printed pairs and decide what's actually the same capacity.
Record decisions by appending to `capability_merges.json`:

```json
[
  {
    "keep": "cap_vulnerability_management_b4c5d6",
    "drop": ["nis2_cap_vulnerability_handling_9f2a1c"],
    "note": "same capacity, NIS2 phrasing"
  }
]
```

Then apply:

```bash
python tools/graph-ingestion/merge_capabilities.py --graph-name policy_system --decisions docs/test-data/eu-regulations/capability_merges.json
# add --dry-run first to preview without writing
```

For each decision, this rewires every edge (any type, any direction —
`REQUIRES` today, `GOVERNED_BY` and the future Policy/Standard/Control
edges tomorrow) touching a dropped node onto the kept node, records the
retired id(s) on the surviving node's `merged_from` property, then deletes
the dropped node. It's idempotent — re-running it after a node's already
been merged just skips it.

`capability_merges.json` is meant to be committed and grown over time: it's
the durable, auditable record of every merge judgment call, so the merged
state can be reproduced by reloading from source JSON and re-running this
script, rather than living only as one-off mutations against a running
graph.
