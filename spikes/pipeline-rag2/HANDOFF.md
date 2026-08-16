# © 2026 Cartman ApS. All rights reserved.
# Handoff for pipeline-rag2 new session

## Context in three lines

- **Goal:** determine if GraphRAG-SDK (FalkorDB) can replace `tools/graph-ingestion`
  for the `policy_system` compliance graph, using "80% SDK + 20% handcrafted
  transformer" rather than a bespoke pipeline.
- **Architecture:** `ingest.py` → `policy_system_graphrag_native` (SDK native:
  `:__Entity__`, `:RELATES` edges) → `transform.py` (deterministic mapper)
  → `cra.json`-format JSON → `load_graph.py` (existing, unchanged) →
  `policy_system_graphrag_final` → convergence + test.
- **The 20% (handcrafted):** `transform.py` (ID assignment, hub-skipping detection,
  cross-ref filtering, property pass-through) and `merge_capabilities.py`
  (existing, unchanged).

Read `README.md` in this folder for the full design. Read `schema.py` for the
ontology (what changed from `pipeline-rag/schema.py` and why).

---

## What's done

| File | Status |
|---|---|
| `README.md` | Done — architecture, key decisions, run instructions |
| `schema.py` | Done — `Attribute` extensions on `Obligation` (confidence FLOAT, obligation_type STRING) and `Capability` (type STRING). Anti-shortcut additions to `Regulation`/`Role` dropped. Direction-explicit relation descriptions kept. `source_ref` as `Attribute` on `DEFINES` and `EXPRESSES` relations. |

---

## What's next, in order

### 1. Verify the `GraphRAG.ingest()` API surface
```bash
python -c "import graphrag_sdk, inspect; print(inspect.signature(graphrag_sdk.GraphRAG.ingest))"
```
Check whether `ingest()` accepts `loader=` and `chunker=` kwargs.
- **If yes** — `ingest.py` can configure `MarkdownLoader` + `StructuralChunking`
  directly via `rag.ingest(...)`.
- **If no** — `IngestionPipeline` must be constructed explicitly with those
  strategies, and `ingest.py` changes to call `IngestionPipeline.run()` or
  equivalent.

### 2. Write `ingest.py`
Adapt `spikes/pipeline-rag/ingest.py` with these changes:
- Default backend: **Azure** `gpt-5.4-mini` + `text-embedding-3-large`
  (Ollama parked — `gemma4:12b` burns full context on reasoning,
  `phi3:mini` has async cold-start bug, both documented in
  `pipeline-rag/LEARNINGS.md`)
- `entity_extractor=LLMExtractor(llm)` in `GraphExtraction`
  (the default `GLiNERExtractor` is wrong for this domain's abstract types)
- `--max-concurrency` default: **2** for Azure (the 10-req/60s rate limit
  caused 9.4% chunk loss at concurrency 3 in the full CRA run; 2 is safe but
  slow — a proper token-bucket rate limiter is a future optimization, not a
  gating concern for this first pass)
- Sources: `.md` files (via `MarkdownLoader` + `StructuralChunking`):
  `docs/regulations/CRA.md`, `docs/regulations/NIS2.md`, `docs/regulations/gdpr.md`,
  `spikes/pipeline-rag/engineering-practices-narrative.md`
  (note: engprac's narrative is in `pipeline-rag`, not yet copied/moved to rag2)
- `--regulation-map` flag: reads `regulation_map.json` to get the canonical
  `document_id` for each source (so we know which `Regulation` node to anchor)
- Output graph: `policy_system_graphrag_native`
- Rate-limit env vars: `AZURE_API_KEY`, `AZURE_API_BASE`, `AZURE_API_VERSION`
  (fetch via `az cognitiveservices account keys list --name
  policy-system-graphrag-spike --resource-group
  rg-policy-system-graphrag-spike --query "key1" -o tsv` — see
  `pipeline-rag/README.md` for the full commands)

### 3. Write `regulation_map.json`
Small data file. Shape:
```json
{
  "CRA-1.0": {
    "canonical_id": "CRA-1.0",
    "document_id": "CRA-1.0",
    "canonical_name": "Cyber Resilience Act",
    "name_variants": ["CRA", "Regulation (EU) 2024/2847", "Cybersecurity Resilience Act"],
    "source_ref_prefix": "CRA-1.0_req_art_",
    "source_file": "docs/regulations/CRA.md"
   },
  "NIS2-1.0": { ... },
  "GDPR-1.0": { ... },
  "ENGPRAC-3.0": { ... }
}
```
Values for `canonical_name`, `name_variants`, `source_ref_prefix` should be
derived from `spikes/graph-ingestion3/cra.json`, `nis2.json`, `gdpr.json` for
consistency with the existing `policy_system` graph.

### 4. Write `transform.py`
The core new piece. Reads `policy_system_graphrag_native` from FalkorDB,
produces `cra.json`-format JSON. Key design decisions already made:
- **Spurious cross-ref filtering:** `regulation_map.json` identifies the primary
  `Regulation` node per document; all other `:__Entity__ {type:'Regulation'}`
  nodes found `MENTIONED_IN` that document are filtered out and listed in
  `hub_edge_report.json` section 1 ("filtered cross-references").
- **Hub-skipping edges** (`Regulation→Capability`, `Regulation→Obligation`,
  `Regulation→PracticeArea` — where `EXPRESSES→SATISFIED_BY→REQUIRES` chain is
  bypassed): listed in `hub_edge_report.json` section 2 ("hub-skipping edges
  omitted"), omitted from the output JSON entirely. No placeholder nodes
  fabricated.
- **ID assignment:** `Regulation` ID from `regulation_map.json`; `Role`,
  `Obligation`, `Capability` etc. get `{prefix}_{slugify(name)}_{sha1(normalised_name).hexdigest()[:6]}`.
  Document the normalisation (lowercase, strip, collapse whitespace, remove
  punctuation) in `transform.py` docstring. This is the convergence-enabling ID
  scheme — different names for the same concept still get different IDs, but
  that's intentional: `find_capability_duplicates.py` + `merge_capabilities.py`
  handle cross-regulation merge, not `transform.py`.
- **Property pass-through:** `confidence`, `obligation_type`, `type` (Capability),
  `source_ref` (on `EXPRESSES`/`DEFINES` edges) read from the native graph
  (populated by `schema.py`'s `Attribute` extensions); missing values defaulted
  to `"active"` for `Requirement.status`, `Capability.status` and `Obligation.status`.
- **Edge type mapping:** native `:RELATES` edges have `rel_type` property; map
  `rel_type` string to the domain edge type (same as in `compare.py` — case
  when `type(r) = 'RELATES'`). Direction correction: apply a static correction
  table for known inversions (e.g. if LLM produces `Capability→RiskPath`
  labelled `MITIGATED_BY`, flip to `RiskPath→Capability`).
- **Output format:** matches `spikes/graph-ingestion3/cra.json` exactly, so
  `load_graph.py` can consume it without changes.
- **`hub_edge_report.json` shape** (two sections, one per regulation
  processed):
  ```json
  {
    "regulation": "CRA-1.0",
    "filtered_cross_references": [{ "name": "...", "from_document": "CRA-1.0" }],
     "hub_skipping_edges": [{ "rel_type": "REQUIRES", "from": "...", "to": "..." }]
  }
  ```

### 5. Write `requirements.txt`
Copy `spikes/pipeline-rag/requirements.txt`:
```
graphrag-sdk[litellm,pdf]
falkordb>=1.7.1
```
(For the new approach, `pdf` extra may not be needed since we ingest `.md`
files via `MarkdownLoader`; verify whether `MarkdownLoader` needs a separate
extra. If `graphrag-sdk[markdown]` or similar exists, use that instead.)

### 6. Write `compare.py`
Adapt `spikes/pipeline-rag/compare.py`. The key change: the test oracle is no
longer just structural + convergence, but also a question-answer quality
check. Use the NP-001..005 from `spikes/pipeline3/smoke-test/run-01-
falsification-pilot.md` and NHQ-1..5 from `run-02-harder-pilot.md` as the
question set. Run each question's Cypher query against
`policy_system_graphrag_final` and compare the answer to the known-good
answer from `policy_system`. This is the step that produces the adoption
decision.

---

## Key facts from `pipeline-rag` (don't re-derive)

| Fact | Value |
|---|---|
| Azure subscription resource group | `rg-policy-system-graphrag-spike` |
| Azure Foundry resource | `policy-system-graphrag-spike` |
| Azure chat deployment | `gpt-5.4-mini` (GlobalStandard, cap=10 — **a rate limit, not a concurrency ceiling**: 10 requests/60s) |
| Azure embedding deployment | `text-embedding-3-large` (Standard) |
| Ollama models | Parked — do not try without root-causing `gemma4:12b`'s reasoning burn and `phi3:mini`'s async cold-start |
| `--max-concurrency` for `ingest.py` | 2 for Azure first pass (3 caused 9.4% loss in full CRA run) |
| `graphrag_sdk` version | 1.4.0 installed |
| Baseline `policy_system` graph | 4 regs, 19 Roles, 287 Requirement, 349 Obligation, 77 Capability, 10 PracticeArea, 6 RiskPath, 10 Policy, 10 Standard, 10 Control — all active |
| `cra.json` node/edge count | Read from `spikes/graph-ingestion3/cra.json` — roughly 100s of edges |
| Convergence in `policy_system` | 50/77 Capabilities (65%) required by ≥2 Obligations across all 4 regulations |

---

## Open design questions (not yet resolved)

1. **Does `GraphRAG.ingest()` accept `loader` and `chunker`?** Must be
   verified before `ingest.py` can be written. See step 1 above.
2. **`regulation_map.json` `name_variants`** — the list of acceptable names
   for each regulation's primary node. Needs to be built from
   `spikes/graph-ingestion3/cra.json` etc. and may need to be extended as
   extraction shows the LLM's naming inconsistencies.
3. **Direction correction table for `transform.py`** — which `rel_type`
   inversions the LLM reliably produces. From `pipeline-rag/LEARNINGS.md`,
   `MITIGATED_BY` and `VERIFIED_BY` are known (LLM produces
   `Capability→RiskPath`; correct is `RiskPath→Capability`). Whether
   `EXPRESSES` (which LLM sometimes produces as `Regulation→Obligation`
   instead of `Regulation→Requirement`) needs a similar correction is
   untested — check in the first `transform.py` dry run.
4. **`--max-concurrency` for a real full-Azure run** — the current
   `cap=10 GlobalStandard` deployment limits real throughput. Whether to
   increase the deployment capacity or implement a client-side token-bucket
   pacer is a cost/effort tradeoff that depends on how often a full
   multi-regulation run is needed. Flag it; don't block on it.

---

## What to NOT re-derive

The `graphrag-sdk-configuration.md` in `spikes/pipeline-rag/docs/` is a
faithful, source-verified catalog of everything configurable in
`graphrag_sdk` 1.4.0. Read it once for the config surface; it's accurate.
The config decisions from this session (in `README.md`'s "Key decisions"
table) are settled — re-opening them means re-running a full experiment
round, not a discussion point.

The `LEARNINGS.md` in `spikes/pipeline-rag/` is a running log that's now
superseded by this folder's design. Individual facts in it are worth
consulting (the Ollama findings, the 71% pruning figure, the 9.4% chunk
loss) but the document itself is not a working spec.
