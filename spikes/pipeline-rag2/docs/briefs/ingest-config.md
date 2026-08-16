# Brief: ingest-config investigation (pipeline-rag2)

READ-ONLY investigation. Do NOT run actual ingestion and make NO Azure calls.
Produce a concise (<=1500 word) findings file + a 10-line summary.

## Goal
Determine the correct **loader + chunker + LLM/extraction wiring** for
`spikes/pipeline-rag2/ingest.py`, specifically the design's new lever:
per-chunk header/section context to reduce hub-skipping.

## Context (read all)
- Design intent + 80/20 thesis + hub-skipping mitigation:
  `spikes/pipeline-rag2/README.md`
- Ontology `SCHEMA`: `spikes/pipeline-rag2/schema.py`
- Proven Azure reference that WORKED (`spikes/pipeline-rag/ingest.py`): note it
  does NOT pass `loader=`/`chunker=` (used SDK-default chunking) and loops one
  path at a time, so ingest-level `max_concurrency` is a **no-op** for it.
- SDK source, installed in `.venv` (find via the paths below):
  - `.venv/lib/python3.14/site-packages/graphrag_sdk/ingestion/loaders/markdown_loader.py`
  - `.venv/lib/python3.14/site-packages/graphrag_sdk/ingestion/chunking_strategies/structural_chunking.py`
  - `.../contextual_chunking.py`
  - `.../fixed_size.py`
  - `.../sentence_token_cap.py`
  - `.../graphrag_sdk/api/main.py` (around `GraphRAG.ingest`, search `def ingest`)
  - `.../graphrag_sdk/ingestion/extraction_strategies/graph_extraction.py`
    (search for `GraphExtraction`, `LLMExtractor`, `max_concurrency`)
- The signature is already known:
  `GraphRAG.ingest(source, *, text, document_id, loader=LoaderStrategy|None,
   chunker=ChunkingStrategy|None, extractor=ExtractionStrategy|None,
   resolver=ResolutionStrategy|None, max_concurrency=3, ctx=None)`
  `GraphRAG.__init__(connection, llm, embedder, ontology=None,
   retrieval_strategy=None, embedding_dimension=256, *, schema=None)`
  `GraphExtraction(..., entity_extractor=LLMExtractor(llm), max_concurrency=...)`
  `LiteLLM(model, timeout=, num_ctx=)`, `LiteLLMEmbedder(model, dimensions=, timeout=)`.
  Azure LLM model string form: f"azure/gpt-5.4-mini"; embed
  f"azure/text-embedding-3-large" dimensions=1536.

## Tasks
1. Confirm `ingest()` accepts `loader=` and `chunker=` (yes, per signature) AND
   determine whether they coexist with `extractor=` — i.e. do loader/chunker
   feed the load+chunk stage and `extractor` the extraction stage? State the
   interaction concretely.
2. For our `.md` regulation files: does `MarkdownLoader` populate
   `document.elements` (structural headings) or produce a flat list? Does that
   let `StructuralChunking` see real headers, or does it fall back to its base
   chunker (the source logs "No structural elements found -> fallback")? If it
   falls back, what is the correct config to obtain per-chunk header/section
   context? Evaluate `ContextualChunking(llm, base_chunker=StructuralChunking(...))`
   vs `StructuralChunking` alone vs neither (SDK default). Give a
   copy-pasteable `ingest(...)` skeleton for the recommended option and note the
   alternative.
3. Give the exact `LiteLLM` / `LiteLLMEmbedder` / `GraphExtraction` /
   `GraphRAG` construction for **Azure** (chat `gpt-5.4-mini`, num_ctx 8192,
   timeouts; embed `text-embedding-3-large` dimensions=1536), using
   `entity_extractor=LLMExtractor(llm)`. Since ingest-level `max_concurrency` is
   a no-op for a single source path, confirm whether `max_concurrency` must be
   enforced on `GraphExtraction`, and (because Azure caps 10 req/60s) propose a
   concrete client-side rate-limiter to hold at **2 concurrent LLM calls**.
4. Flag every assumption/uncertainty with an explicit confidence level
   (low/med/high) and the evidence.

## Output
Write `spikes/pipeline-rag2/docs/ingest-config-findings.md` with:
- a short verdict on loader/chunker choice (with the MarkdownLoader→flat-list
   risk and whether ContextualChunking is needed);
- a copy-pasteable `ingest(...)` skeleton (recommended + alternative);
- the Azure LLM/extraction wiring;
- the rate-limiter proposal;
- an "assumptions / open items" list with confidence levels.
End your tool-result message with a **10-line plain-text summary** of the
recommendations (this is what the leader reads back).
