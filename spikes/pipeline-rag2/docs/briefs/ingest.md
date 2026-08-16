# Brief: write ingest.py + RateLimitedLLM (pipeline-rag2, sub-agent #2)

Produce TWO source files. Do NOT run the full ingestion or make REAL Azure
calls. You MAY statically verify (python compile / `--help` / a mock-LLM dry
run). Write a concise RUN note with the exact leader commands.

## Authoritative inputs (read these first)
- `spikes/pipeline-rag2/docs/ingest-config-findings.md` — the validated blueprint
  (use it; do not contradict it).
- `spikes/pipeline-rag2/schema.py` — `SCHEMA` (the ontology passed to GraphRAG).
- `spikes/pipeline-rag/ingest.py` — proven REFERENCE for arg structure,
   Azure/ollama backend split, `ConnectionConfig`/falkordb wiring, finalize(),
   and the "ingest-level max_concurrency is a no-op" handling. Adapt, don't copy.
- `docs/regulations/CRA.md` and `spikes/pipeline-rag2/engineering-practices-narrative.md` —
  just to confirm INPUT file format (Markdown). Do not ingest them.
- SDK under `spikes/pipeline-rag2/.venv/lib/python3.14/site-packages/graphrag_sdk/`.

## File 1: `spikes/pipeline-rag2/ratelimit.py`
`RateLimitedLLM(inner, concurrency=2, req_per_window=10, window_s=60.0)` wrapping
an inner `graphrag_sdk.LiteLLM`, implementing the **FULL** `LLMInterface` ABC
(`invoke, ainvoke, ainvoke_messages, abatch_invoke, ainvoke_with_model, astream,
invoke_with_model`) so it can be injected anywhere `llm=` is taken. Enforce a
SHARED GLOBAL cap across all call sites: an `asyncio.Semaphore(concurrency)`
(2 concurrent) AND a sliding 10-per-60s bucket, both by reference (one instance
shared). `abatch_invoke` must serialize through the semaphore (do NOT propagate
its `max_concurrency=` — the shared semaphore is the single gate) and return
`list[LLMBatchItem]` like the inner. NO new dependency (pure asyncio; do not
import `aiolimiter` — just note it as a cleaner alternative in a comment).
Add a tiny `__main__` self-test that runs a fake inner LLM through concurrency
and asserts 10 calls in <60s are throttled (no Azure).

## File 2: `spikes/pipeline-rag2/ingest.py`
Per the blueprint + reference:
- Args: `--source {cra,nis2,gdpr,engprac,all}`, `--regulation-map PATH`
   (optional: if given, drives per-source document_id + source_file; see
   regulation_map.json shape — draft it to be tolerant when absent),
   `--backend azure` (default; keep an ollama branch stubbed like the reference),
   `--graph-name` default `policy_system_graphrag_native`,
   `--reset` (clean the target graph before ingest for a reproducible re-run),
   `--max-chunks N` (optional: cap extraction to N total chunks for a dry run —
   implement via a chunker wrapper that truncates, since `ingest()` has no
   such kwarg), `--max-concurrency` default **2**.
- Build: `LiteLLM(model="azure/gpt-5.4-mini", api_key/base/version from env
   AZURE_API_KEY/AZURE_API_BASE/AZURE_API_VERSION, timeout=1800.0)`;
   `LiteLLMEmbedder(model="azure/text-embedding-3-large", dimensions=1536,
   timeout=1800.0)`; wrap the LLm in `RateLimitedLLM(...)` and inject that
   wrapper as `llm=` into `GraphExtraction`, `ContextualChunking` (if used),
   AND `GraphRAG`.
- `chunker = StructuralChunking(max_tokens=512, overlap_sentences=2)` (MANDATORY),
   `loader = MarkdownLoader()`, `extractor = GraphExtraction(llm=<wrapper>,
   entity_extractor=LLMExtractor(<wrapper>), max_concurrency=2)`.
- `GraphRAG(connection=<per reference>, llm=<wrapper>, embedder,
   ontology=SCHEMA, embedding_dimension=1536)`.
- **DO NOT pass `num_ctx`** (Ollama-only; gpt-5* reasoning path). Embed dim MUST be 1536.
- Loop sources ONE AT A TIME: `await rag.ingest(source=str(path),
   document_id=..., loader=..., chunker=..., extractor=...)` then
   `await rag.finalize()`.
- `--reset`: reset/clean the target graph via the falkordb API (mirror
   how `--reset` works in `spikes/pipeline-rag/ingest.py` / `load_graph.py`).
- **Semantic/structured logging** to `spikes/pipeline-rag2/logs/ingest-<ts>.jsonl`:
   one record at start, one per source (chunk count, LLM call count if available,
   extracted node/edge counts from the result, wall-time), and one on error —
   enough to debug a failed run from the log alone.
- `__main__`: argparse + asyncio driver.

## Verification (NO real Azure)
- `python -m py_compile` both files.
- `python ingest.py --help` succeeds.
- If you can construct a fake inner LLM/embedder, run a 1-chunk mock dry-run
   (`--max-chunks 1`) against a tiny temp .md to prove the wiring runs — but
   using FAKE in-process doubles, NOT real Azure. State clearly what was faked.

## Return
A 10-line summary: exactly which 2 files, the key wiring decisions, what the
self-tests proved, AND the two exact shell commands the leader will run:
(a) the 1-chunk Azure dry-run, (b) the full CRA ingest.
