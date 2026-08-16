# Brief: write `spikes/pipeline-rag2/ingest.py`  (sub-agent, model=qwen3:14b)

## HARD RULES
- Write EXACTLY ONE file: `spikes/pipeline-rag2/ingest.py`. Do not modify anything else.
- `spikes/pipeline-rag2/ratelimit.py` ALREADY EXISTS and is CORRECT. `import RateLimitedLLM from ratelimit` — DO NOT modify or rewrite it.
- DO NOT make any real Azure/network call. DO NOT reset/delete any FalkorDB graph.
- Concise code: target <=250 lines, minimal docstrings.
- Keep your context small: you MAY read `spikes/pipeline-rag2/docs/ingest-config-findings.md` for the *reasoning*, but the exact wiring below is authoritative — use it.

## Imports (exact)
    from graphrag_sdk import (ConnectionConfig, GraphExtraction, GraphRAG, LLMExtractor,
                              LiteLLM, LiteLLMEmbedder)
    from graphrag_sdk.ingestion.chunking_strategies.structural_chunking import StructuralChunking
    from graphrag_sdk.ingestion.loaders.markdown_loader import MarkdownLoader
    from ratelimit import RateLimitedLLM          # this spike's module; same dir
    from schema import SCHEMA                      # this spike's ontology; same dir

## Wiring (authoritative — from a validated source investigation)
- Azure LLM + embedder from ENV: `AZURE_API_KEY`, `AZURE_API_BASE`, `AZURE_API_VERSION`.
    llm_raw    = LiteLLM( model="azure/gpt-5.4-mini",
                          api_key=os.environ["AZURE_API_KEY"],
                          api_base=os.environ["AZURE_API_BASE"],
                          api_version=os.environ["AZURE_API_VERSION"],
                          timeout=1800.0)
    embedder   = LiteLLMEmbedder( model="azure/text-embedding-3-large",
                                  api_key=os.environ["AZURE_API_KEY"],
                                  api_base=os.environ["AZURE_API_BASE"],
                                  api_version=os.environ["AZURE_API_VERSION"],
                                  dimensions=1536, timeout=1800.0)
    llm = RateLimitedLLM(llm_raw, concurrency=args.max_concurrency,
                         req_per_window=10, window_s=60.0)   # SHARED global cap
- DO NOT pass `num_ctx` anywhere (Ollama-only; gpt-5* is a reasoning path).
- chunker    = StructuralChunking(max_tokens=512, overlap_sentences=2)
- extractor  = GraphExtraction(llm=llm, entity_extractor=LLMExtractor(llm),
                               max_concurrency=args.max_concurrency)
- builder    = GraphRAG(connection=<falkordb connection, per reference>,
                        llm=llm, embedder=embedder,
                        ontology=SCHEMA, embedding_dimension=1536)
- Per source, ONE AT A TIME:  await builder.ingest(source=str(path), document_id=did,
        loader=MarkdownLoader(), chunker=chunker, extractor=extractor);  then after all:
        `await builder.finalize()`.

## CLI / args
- `--source {cra,nis2,gdpr,engprac,all}` (maps to docs/regulations/CRA.md, NIS2.md,
   gdpr.md, and spikes/pipeline-rag2/engineering-practices-narrative.md for engprac).
- `--regulation-map PATH` OPTIONAL: JSON {canon: {... document_id, source_file ...}}.
   If present, it drives per-source document_id (+ source_file) and MUST be tolerant
   when a source has no entry (fall back to source filename as document_id).
- `--backend {azure}` default azure (an ollama branch may be stubbed but NOT required).
- `--graph-name` default `policy_system_graphrag_native`.
- `--max-concurrency` default 2.
- `--reset` : before ingesting, clean/emptY the target graph via the falkordb API
   (mirror `--reset` behavior in `spikes/pipeline-rag/ingest.py`).
- `--max-chunks N` OPTIONAL DRY-RUN: cap total extracted chunks to N by wrapping the
   chunker so it yields at most N chunks (the SDK `ingest()` has no such kwarg).
   Default: no cap.

## Semantic logging
- Write NEWLINE-DELIMITED JSON to `spikes/pipeline-rag2/logs/ingest-<ts>.jsonl`:
  one record at start; one per source {source, document_id, wall_s, chunks, nodes_added,
   edges_added (from the ingest result)}; one final record or an {level:error, traceback}
   record on failure. Enough that a person can debug a failed run from the log alone.

## Verification you MUST run yourself, BEFORE finishing (NO real Azure):
1. `python -m py_compile ingest.py`
2. `python ingest.py --help`
3. A MOCK 1-chunk dry-run: inject fake in-process `LiteLLM`/`LiteLLMEmbedder` doubles
   (subclass/replace, returning canned LLMResponse + 1536-dim list) and `--max-chunks 1`
   pointing at a tiny temp markdown, writing to a throwaway graph name, proving the
   wiring + the chunk-truncation run. State in your summary exactly what was faked.
   DO NOT touch the real graphs (policy_system, *_native, *_final).

## End your message with an 8-10 line summary:
- which file you wrote and its line count;
- the exact leader commands for (a) the 1-chunk AZURE dry-run and (b) the FULL CRA ingest,
   INCLUDING the env exports they need (AZURE_API_KEY etc. — you may reference them symbolically, do NOT print any real key);
- what your mock dry-run proved and what remains to be confirmed on a real first ingest.
