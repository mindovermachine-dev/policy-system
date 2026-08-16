<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: PDF → GraphRAG-SDK native graph (CRA only)

**Status:** Complete, 2026-08-16. See `docs/ac3-analysis.md`. Recommendation: proceed to pipeline-rag4.

## Purpose

Test whether GraphRAG-SDK can ingest an EU regulation **PDF directly**
(not the pre-converted `.md` files) into a graph, using GraphRAG-SDK's own
native graph meta-model — `:__Entity__` nodes, `:RELATES` edges with a
`rel_type` property — while the LLM extraction is made aware of the
`policy_system` domain meta-model (via `schema.py`'s entity/relation
descriptions) without forcing the *output* into that domain shape.

This is extraction only. Mapping the native graph into a `policy_system`-
shaped graph is out of scope here — see `spikes/pipeline-rag4/`.

## Scope

- **Regulation:** CRA only (`docs/regulations/CRA.pdf`). GDPR and NIS2
  are explicitly out of scope until CRA ingestion is judged to work.
- **Input:** the PDF directly. Not `docs/regulations/CRA.md`.
- **graphSDKConfiguration:** Consult the graphrag-sdk-configuration.md to fully understand how to configure the graphSDK
- **Output:** a native GraphRAG-SDK graph in FalkorDB (working name:
  `policy_system_graphrag_native`).
- **Not in scope:** mapping/transforming into domain shape, loading into
  `policy_system`, GDPR/NIS2.

## Acceptance criteria

**"Has CRA been ingested?"** — deliberately not automated/scored yet.

1. Run ingestion against `CRA.pdf`, land a non-trivial graph in
   `policy_system_graphrag_native`.
2. `compare.py` (in this spike) gives a first structural read against the
   `policy_system` baseline — informational only, not pass/fail.
3. Manual inspection of the resulting graph determines whether ingestion
   is good enough to proceed to pipeline-rag4, and what an automated bar
   should look like going forward.

No fixed quality threshold (entity counts, pruning %, etc.) is set in
advance.

## Baseline for comparison

`policy_system` graph as produced by `tools/graph-ingestion/load_all.sh`
from `test-data/eu-regulations/*.json`.

## File inventory

| File | Purpose | Status |
|---|---|---|
| `README.md` | This document | Done |
| `ingest.py` | GraphRAG-SDK ingestion of `CRA.pdf` into `policy_system_graphrag_native` | Done |
| `schema.py` | Ontology giving the LLM domain-meta-model awareness without shaping output | Done |
| `requirements.txt` | Python dependencies | Done |
| `compare.py` | Structural comparison against the `policy_system` baseline | Done |
| `graphrag-sdk-configuration.md` | SDK configuration options | Reviewed |
| `docs/ac3-analysis.md` | AC3 manual inspection data + recommendation | Done |
