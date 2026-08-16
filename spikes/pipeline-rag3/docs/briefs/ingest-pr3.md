# Brief: write `spikes/pipeline-rag3/ingest.py`  (sub-agent; model=qwen3-coder-next)

## HARD RULES
- Write EXACTLY ONE file: `spikes/pipeline-rag3/ingest.py`. Modify nothing else.
- Template = `spikes/pipeline-rag2/ingest.py` — READ IT FULLY first; it is proven.
- `spikes/pipeline-rag3/ratelimit.py` and `spikes/pipeline-rag3/schema.py`
   ALREADY EXIST and are CORRECT. `from ratelimit import RateLimitedLLM` and
   `from schema import SCHEMA` (same dir). DO NOT rewrite or touch them.
- DO NOT make ANY real Azure/network call. DO NOT reset/delete ANY FalkorDB graph.
- Target <= 270 lines, minimal docstrings, concise.

## De-scoping changes vs pr2 template (this is CRA-only, native-output)
- `SOURCES` = CRA ONLY:
    `{"cra": {"path": REPO_ROOT/"docs/regulations/CRA.pdf", "document_id": "CRA-1.0", "kind": "pdf"}}`.
   Remove nis2/gdpr/engprac entirely.
- `--source`: choices `["cra"]`; default `cra`. (Drop `all`/multi-reg.)
- `--graph-name` default = `policy_system_graphrag_native`.
- KEEP unchanged from pr2:
   * Azure LLM: `LiteLLM(model="azure/gpt-5.4-mini", api_key=os.environ["AZURE_API_KEY"],
     api_base=os.environ["AZURE_API_BASE"], api_version=os.environ["AZURE_API_VERSION"], timeout=1800.0)`.
   * Embedder: `LiteLLMEmbedder(model="azure/text-embedding-3-large", same 3 env vars,
     dimensions=1536, timeout=1800.0)`.
   * `_require_azure_env()` guards all 3 env vars; call it in `run()` before the pipeline.
   * `llm = RateLimitedLLM(llm_raw, concurrency=args.max_concurrency, req_per_window=10, window_s=60.0)`.
   * chunker: default `SentenceTokenCapChunking(max_tokens=512, overlap_sentences=2)`.
   * `--max-chunks` -> `CappedChunker`; `--substantive N` + `--filter-regex` (default
     `shall|should`) + `--spread` -> `FilteringChunker`. (keep both wrappers verbatim).
   * `extractor = GraphExtraction(llm=llm, entity_extractor=LLMExtractor(llm),
     entity_types=[e.label for e in SCHEMA.entities], max_concurrency=args.max_concurrency)`.
   * `GraphRAG(connection=ConnectionConfig(host,port,graph_name), llm=llm, embedder=embedder,
     ontology=SCHEMA, embedding_dimension=1536)`; per-source loop over `keys`, then `await rag.finalize()`.
   * `--reset` scoped to `--graph-name` only (mirror pr2 reset_graph; it may not touch other graphs).
   * `--max-concurrency` default 2. `--host`/`--port` default localhost:6379.
   * Semantic JSONL logging to `LOG_DIR=SPIKE_DIR/"logs"` append-mode, events
     start/source_done/summary(+error). Keep the RateLimitedLLM/stats and
     chunk_select deltas from pr2.
- Native output is implicit (the SDK writes `:__Entity__`/`:RELATES`); NO transform
   step, NO JSON export — pr3 is extraction-only.

## Imports (exact, from pr2)
    from graphrag_sdk import (ConnectionConfig, GraphExtraction, GraphRAG, LLMExtractor,
                              LiteLLM, LiteLLMEmbedder)
    from graphrag_sdk.core.models import TextChunks
    from graphrag_sdk.ingestion.chunking_strategies.base import ChunkingStrategy
    from graphrag_sdk.ingestion.chunking_strategies.sentence_token_cap import SentenceTokenCapChunking
    from graphrag_sdk.ingestion.loaders.pdf_loader import PdfLoader
    from ratelimit import RateLimitedLLM
    from schema import SCHEMA
    from falkordb import FalkorDB
(Note: pr3 uses PDF only, so MarkdownLoader import is not required.)

## Exit criteria (self-check BEFORE finishing; report each explicitly)
1. `spikes/pipeline-rag3/ingest.py` exists.
2. `python -c "import ast; ast.parse(open('spikes/pipeline-rag3/ingest.py').read())"` passes.
3. `spikes/pipeline-rag3/.venv/bin/python spikes/pipeline-rag3/ingest.py --help` runs and lists all flags
   (run with cwd = spikes/pipeline-rag3 so `schema`/`ratelimit` import resolves).
4. NO Azure/network call was made; NO FalkorDB graph was created/reset/deleted.
Report the exact commands you ran and their output for 2 & 3.
