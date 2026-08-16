<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: Pipeline via GraphRAG-SDK (revised)

**Status:** Design, 2026-08-14. Supersedes the running-log approach in
`spikes/pipeline-rag/`.

## Why this replaces `spikes/pipeline-rag`

`pipeline-rag` tried to get GraphRAG-SDK's LLM extraction to produce
a graph that directly matches the `policy_system` domain model — wrong
layer for that constraint. The result: 71% of extracted relationships
pruned, the schema wording iterated but regressed (38→49 pruned, wrong
variant live in `schema.py`), convergence untested, Ollama backend
parked on two distinct failure modes, Azure backend working but with
9.4% chunk loss from rate limiting. The findings are real and
documented in `spikes/pipeline-rag/LEARNINGS.md`, but they describe a
system that was fighting its own design.

The pivot: **GraphRAG-SDK is the extraction layer; `load_graph.py`
and `merge_capabilities.py` are the model layer.** The hard, LLM-dependent
work (reading regulation prose, identifying entities and their
relationships from it) is what GraphRAG-SDK is good at. The domain-model
specific work (ID convention, chain completeness, cross-regulation
convergence) is deterministic and already exists in `tools/graph-ingestion`.

## Architecture

```
docs/regulations/*.md
  CRA.pdf, NIS2.pdf, GDPR.pdf, engineering-practices-narrative.md
  (MarkdownLoader + StructuralChunking — unlocks header-breadcrumb context)
        ↓
 ingest.py   (GraphRAG-SDK, Azure gpt-5.4-mini native)
        ↓
 policy_system_graphrag_native   (FalkorDB — intermediate, SDK-native shape:
   :__Entity__ nodes, :RELATES edges with rel_type property)
        ↓
 transform.py   ← NEW: the 20%
   - reads native graph, produces same JSON shape as cra.json
   - filters spurious cross-ref Regulation nodes (by known canon. ID)
   - maps :RELATES+rel_type → typed domain edges
   - assigns domain model IDs (cap_{slug}_{hash}, role_{slug}_{hash}, etc.)
   - flags hub-skipping edges in hub_edge_report.json (omits from output)
        ↓
 output/cra.json   etc.   (same shape load_graph.py already consumes)
        ↓
 load_graph.py   (existing, tools/graph-ingestion, unchanged)
        ↓
 policy_system_graphrag_final   (FalkorDB — domain-model shape)
        ↓
 find_capability_duplicates.py  (existing, unchanged)
 merge_capabilities.py          (existing, unchanged)
        ↓
 compare.py   (test oracle: structural + convergence + content fidelity +
   run N real compliance questions from pipeline3's NP/NHQ sets,
   compare answers to policy_system's known-good results)
```

## Key decisions (from this session's exploration)

| Decision | Chosen | Rationale |
|---|---|---|
| Extraction layer | GraphRAG-SDK, `GraphExtraction(llm, entity_extractor=LLMExtractor(llm))` | `pipeline-rag`'s `GLiNERExtractor` default is wrong for this domain — `LLMExtractor` routes abstract entity types (PracticeArea, RiskPath, Capability, Obligation) through the chat LLM that already has the per-type descriptions |
| Chunking | `MarkdownLoader` + `StructuralChunking` | Header-breadcrumb context per chunk is a direct lever against hub-skipping; `StructuralChunking` silently inert against PDF input (`PdfLoader` never populates `document.elements`) |
| Backend | Azure `gpt-5.4-mini` + `text-embedding-3-large` | Ollama's `gemma4:12b` burns full 8192 token context on reasoning (finish_reason=length, 0 relationships); `phi3:mini` has an async cold-start bug |
| Schema | `schema.py` with direction-explicit relation descriptions; drop `Regulation`/`Role` anti-shortcut additions | The 38-pruned config (direction-only) is proven better than the 49-pruned anti-shortcut variant; anti-shortcut diluted the direction guidance |
| Domain properties (`confidence`, `obligation_type`, `type`, `status`) | LLM produces them via `schema.py` `Attribute` extensions; transformer passes through | These are LLM-judgment properties; a fixed default would fake what the LLM can produce |
| Hub-skipping edges (`Regulation→Capability` direct) | Flag in `hub_edge_report.json`, omit from output JSON | Fabricating placeholder nodes breaks the "LLM extracts, code maps" invariant; the report is enough for a human to decide |
| Cross-regulation convergence | `find_capability_duplicates.py` + `merge_capabilities.py` unchanged | TF-IDF cosine + human review is already domain-appropriate; no SDK resolver is needed for this |
| Spurious cross-ref `Regulation` nodes | Filter by known canonical regulation ID; list unmatched in report | The LLM's `Regulation` extraction will create nodes for every regulation cited within the source text; a human-readable mapping file (`regulation_map.json`) identifies which is the primary one, everything else is filtered |

## File inventory

| File | Purpose | Status |
|---|---|---|
| `README.md` | This document. Clean replacement for `pipeline-rag/LEARNINGS.md`'s role | Done |
| `ingest.py` | GraphRAG-SDK ingestion into `policy_system_graphrag_native` | To write |
| `transform.py` | Native graph → `cra.json`-format JSON + `hub_edge_report.json` | To write — the new 20% |
| `schema.py` | Ontology with `Attribute` extensions for `confidence`, `obligation_type`, `type`, `status`; direction-explicit descriptions; no anti-shortcut | To write — derived from `pipeline-rag/schema.py` |
| `regulation_map.json` | Maps `document_id → {canonical_id, canonical_name, source_ref_prefix}` | To write |
| `compare.py` | Test oracle: structural + convergence + question answer quality | To write — adapted from `pipeline-rag/compare.py` |
| `requirements.txt` | Same as `pipeline-rag/requirements.txt` | To write |

## Run

```bash
cd ../..    # repo root
pip install -r spikes/pipeline-rag2/requirements.txt

# Step 1: ingest each source document into the native intermediate graph
export AZURE_API_KEY=$(az cognitiveservices account keys list \
  --name policy-system-graphrag-spike \
  --resource-group rg-policy-system-graphrag-spike \
  --query "key1" -o tsv)
export AZURE_API_BASE="https://policy-system-graphrag-spike.openai.azure.com/"
export AZURE_API_VERSION="2024-10-21"

python spikes/pipeline-rag2/ingest.py \
  --source cra --regulation-map spikes/pipeline-rag2/regulation_map.json \
  --backend azure --graph-name policy_system_graphrag_native

# Repeat for nis2, gdpr, engprac (each into the same native graph)

# Step 2: transform native → domain-model JSON
python spikes/pipeline-rag2/transform.py \
  --regulation-id CRA-1.0 \
  --regulation-map spikes/pipeline-rag2/regulation_map.json \
  --graph-name policy_system_graphrag_native \
  --output-dir spikes/pipeline-rag2/output/

# Step 3: load into the final domain-model graph
python tools/graph-ingestion/load_graph.py \
  --file spikes/pipeline-rag2/output/cra.json \
  --graph-name policy_system_graphrag_final \
  --reset

# Step 4: convergence (run after all 4 regulations loaded)
python tools/graph-ingestion/find_capability_duplicates.py \
  --graph-name policy_system_graphrag_final
python tools/graph-ingestion/merge_capabilities.py \
  --decisions spikes/pipeline-rag2/capability_merges.json \
  --graph-name policy_system_graphrag_final

# Step 5: test
python spikes/pipeline-rag2/compare.py
```

## Graphs in FalkorDB

| Graph | Produced by | Shape |
|---|---|---|
| `policy_system` | `tools/graph-ingestion/load_graph.py` from `cra.json` etc. | Domain model, hand-curated from `spikes/graph-ingestion3` |
| `policy_system_graphrag_native` | `ingest.py` | GraphRAG-SDK native (`:__Entity__`, `:RELATES`) |
| `policy_system_graphrag_final` | `load_graph.py` from transform output | Domain model, automated |

## What this is NOT

- Not a replacement for `find_capability_duplicates.py` /
  `merge_capabilities.py` — those are the convergence layer and are
  unchanged and sufficient.
- Not a new ingestion paradigm for the final graph — `load_graph.py`
  is untouched; the new work is entirely in `ingest.py` + `transform.py`.
- Not a full pipeline for all EU regulations yet — the first pass is
  `CRA` only, then validate, then extend.
