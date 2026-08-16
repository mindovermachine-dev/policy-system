# ingest-config findings — pipeline-rag2

READ-ONLY source investigation (no ingestion run, no Azure calls). SDK
`graphrag-sdk` v1.x in `spikes/pipeline-rag2/.venv` (python 3.14; litellm
1.97.0, tiktoken 0.13.0, gliner 0.2.28, markdown-it-py 4.2.0, falkordb
1.7.1). All line refs are to that installed source.

## 1. Verdict: loader / chunker / extractor interaction

**`loader=` and `chunker=` coexist with `extractor=` — they are three
independent, per-call strategy slots.** `ingest()` (api/main.py:1338) routes
to `IngestionPipeline.run` (ingestion/pipeline.py), which executes a fixed
sequence:
- Step 1 load → `self.loader.load(source, ctx)` (pipeline.py:162)
- Step 2 chunk → **`self.chunker.chunk_document(document, ctx)`** (pipeline.py:178)
- Step 4 extract → `self.extractor.extract(chunks, ontology, ctx)` (pipeline.py:190)

So **loader+chunker feed the load+chunk stage; `extractor` feeds extraction.**
A non-None `chunker` fully replaces the default; passing `loader=`/`chunker=`/
`extractor=` together is supported and intended. Defaults when omitted
(_ingest_single, main.py:1588-1590): `TextLoader()`/extension-detected loader,
`SentenceTokenCapChunking(max_tokens=512, overlap=2)`, `GraphExtraction(
llm, entity_types=ontology.entities)` with the **local GLiNER step-1 NER** and
no `max_concurrency`.

**The `MarkdownLoader` → flat-list risk (Task 2):** `MarkdownLoader._load_sync`
(markdown_loader.py) **does** populate `document.elements` — a *flat* list of
`DocumentElement`s (types `header, paragraph, list, table, code, blockquote`),
each carrying `breadcrumbs` = its header hierarchy. It is flat, not nested, but
that is fine. `StructuralChunking.chunk_document` (structural_chunking.py)
**reads `document.elements`** and prefixes every non-header element with
`" > ".join(breadcrumbs)` + its content — **this is exactly the per-chunk
header/section context lever.** Therefore:
- For `.md` files the source **will NOT log "No structural elements found →
  fallback."** That log only fires from the flat-text `chunk()` path, which is
  hit only when `document.elements` is empty (i.e. `PdfLoader`, which never
  populates `elements`, or an empty doc). So the "fallback" fear in the brief is
  **a non-issue for markdown** (confidence: HIGH).
- O-sized element handling: a single element exceeding `max_tokens` is delegated
  to the fallback chunker (`SentenceTokenCapChunking`) with the breadcrumb
  prefix re-attached — graceful, not data loss.

**Critical gotcha:** the SDK **default is `SentenceTokenCapChunking`, which does
NOT override `chunk_document`** (base.py `chunk_document` → `chunk(document.text)`),
so **it discards `document.elements` and splits raw text by sentence with no
header context.** The hub-skipping mitigation only materializes if you **explicitly
pass `chunker=StructuralChunking(...)`**. `.md` auto-selects `MarkdownLoader`
(`_default_loader_for`, main.py:1765) so `loader=` is optional but recommended
for explicitness.

**ContextualChunking vs StructuralChunking vs SDK-default:**
- `StructuralChunking` — deterministic breadcrumbs, **zero extra LLM calls**.
- `ContextualChunking(llm, base_chunker=StructuralChunking(...))` — adds
  **one LLM "context-prefix" call per chunk** (Anthropic contextual retrieval).
  `_enrich_chunks` calls `self.llm.abatch_invoke(prompts)` with **no
  `max_concurrency`** (contextual_chunking.py) → falls back to the
  `LLMInterface` default of **12 concurrent** calls, *bypassing* any cap you
  set on `GraphExtraction`. With Azure's 10 req/60s cap this is a rate-limit
  landmine, and it multiplies chat calls per chunk.

**Recommended: `StructuralChunking` alone.** It delivers the breadcrumb context
lever at **no extra call cost** and no rate-limit risk. Keep
`ContextualChunking` as the *alternative* (see below) only if retrieval quality
justifies the added cost — and then it **must** run through the rate-limiter
wrapping the LLM (Task 3), because its enrichment path is not governed by
`GraphExtraction.max_concurrency`.

## 2. Copy-pasteable `ingest(...)` skeleton

Import note (confidence: HIGH): `StructuralChunking` and `MarkdownLoader` are
**NOT** top-level exports (`__init__.py`); import from module paths. `GraphExtraction`,
`LLMExtractor`, `ContextualChunking`, `LiteLLM`, `LiteLLMEmbedder`,
`ConnectionConfig`, `GraphRAG` **are** top-level.

### Recommended — `StructuralChunking` (no extra LLM calls)
```python
from graphrag_sdk import (
    ConnectionConfig, GraphExtraction, GraphRAG, LLMExtractor,
    LiteLLM, LiteLLMEmbedder,
)
from graphrag_sdk.ingestion.chunking_strategies.structural_chunking import StructuralChunking
from graphrag_sdk.ingestion.loaders.markdown_loader import MarkdownLoader
from schema import SCHEMA

# ... llm, embedder built per Task 3 (rate-limited wrapper injected as llm) ...

chunker  = StructuralChunking(max_tokens=512, overlap_sentences=2)   # breadcrumb-context chunks
extractor = GraphExtraction(
    llm=llm,                                   # the rate-limited LLM wrapper
    entity_extractor=LLMExtractor(llm),        # LLM step-1 NER, NOT the default GLiNER
    # entity_types=... OPTIONAL/REUNDANT: GraphExtraction overrides with
    # ontology.entities when a non-empty ontology is passed to GraphRAG
    max_concurrency=2,                          # caps step1+step2, but NOT chunking (see §3)
)

async with GraphRAG(
    connection=connection,
    llm=llm,
    embedder=embedder,
    ontology=SCHEMA,                # drives entity_types + attribute + relation prompts
    embedding_dimension=1536,       # MUST match LiteLLMEmbedder(dimensions=1536)
) as rag:
    result = await rag.ingest(
        source=str(path),           # e.g. docs/regulations/CRA.md
        document_id="CRA-1.0",      # stable id; omit → os.path.normpath(source)
        loader=MarkdownLoader(),    # == auto-detected for .md, explicit for intent
        chunker=chunker,
        extractor=extractor,
        # max_concurrency=... is a NO-OP here (single path). Do NOT rely on it.
    )
# loop one source at a time, then: await rag.finalize()
```

### Alternative — `ContextualChunking` (adds LLM context prefixes; rate-limit guarded)
```python
# Replace only the chunker; route context-gen through the shared rate-limited LLM.
chunker = ContextualChunking(
    llm=llm,                          # rate-limited wrapper → its abatch_invoke is bounded
    base_chunker=StructuralChunking(max_tokens=512, overlap_sentences=2),
)
# extractor unchanged (already bounded via §3 wrapper despite max_concurrency)
```
`ContextualChunking` with a `base_chunker` forbids the shorthand kwargs
(`max_tokens/overlap_sentences/encoding_name`) — configure those on the base
chunker only (contextual_chunking.py docstring).

## 3. Azure LLM / extraction wiring + client-side rate limiter

### Construction (chat `gpt-5.4-mini` @ num_ctx 8192; embed `text-embedding-3-large` dim 1536)
```python
llm_raw = LiteLLM(
    model=f"azure/gpt-5.4-mini",
    api_key=AZURE_API_KEY, api_base=AZURE_API_BASE, api_version=AZURE_API_VERSION,
    timeout=1800.0,                 # Ollama-tuned; see gotcha G-1
)
embedder = LiteLLMEmbedder(
    model="azure/text-embedding-3-large",
    api_key=AZURE_API_KEY, api_base=AZURE_API_BASE, api_version=AZURE_API_VERSION,
    dimensions=1536, timeout=1800.0,
)
llm = RateLimitedLLM(llm_raw, concurrency=2, req_per_window=10, window_s=60.0)
```
`GraphExtraction(llm=llm, entity_extractor=LLMExtractor(llm), max_concurrency=2)`
and `GraphRAG(llm=llm, ...)`. `LLMExtractor(llm)` routes abstract types
(PracticeArea/RiskPath/Capability/Obligation) through the chat LLM — required;
the default step-1 is local `GLiNERExtractor` (wrong for this domain).

**Confirm: `max_concurrency` MUST be set on `GraphExtraction`, not `ingest`.**
In `ingest()`, `max_concurrency` only governs `_ingest_batch` (list source) — a
per-document `asyncio.Semaphore` (main.py:1636). For a **single path it is a
no-op** (confidence: HIGH). Per-chunk concurrency is set by
`GraphExtraction._max_concurrency`, forwarded to `llm.abatch_invoke(
..., max_concurrency=N)` for both step-1 NER and step-2 verify+rels; if left
`None` it falls back to the `LLMInterface` default **12** (base.py:117).

**Why a wrapper, not just per-strategy `max_concurrency`: two bypasses.**
1. `ContextualChunking._enrich_chunks` calls `self.llm.abatch_invoke(prompts)`
   with **no `max_concurrency`** → unbounded (default 12), independent of
   `GraphExtraction`.
2. `abatch_invoke` builds a **fresh `asyncio.Semaphore` per call**
   (base.py:285) — each call site's cap is *local*, not a global pool, so N
   concurrent call sites ⇒ N×N possible in-flight calls.
Both bypass any `GraphExtraction.max_concurrency`. The only way to hold a *global*
"max 2 concurrent LLM calls / ≤10 per 60s" across extraction **and** contextual
chunking is a **single shared wrapper** injected everywhere `llm=` is taken.

### Rate-limiter proposal (holds 2 concurrent chat calls + 10/60s throughput)
10 req/60s is *throughput*; "2 concurrent" is *concurrency* — enforce **both**
(2 calls of ~1-3s = ~40-120 req/min, overrunning 10/60s): one
`asyncio.Semaphore(2)` + sliding/token bucket, **shared by reference**: 
```python
import asyncio, time
from graphrag_sdk.core.providers.base import LLMInterface, Embedder
from graphrag_sdk.core.models import LLMResponse, ChatMessage

class RateLimitedLLM(LLMInterface):
    """Shared global cap: 2 concurrent chat calls, <=10 per 60s, per process."""
    def __init__(self, inner, concurrency=2, req_per_window=10, window_s=60.0):
        super().__init__(model_name=inner.model_name, max_concurrency=concurrency)
        self._inner = inner; self._csem = asyncio.Semaphore(concurrency)
        self._stamp: list[float] = []; self._rq = asyncio.Semaphore(req_per_window)
        self._lock = asyncio.Lock()

    async def _throttle(self):
        async with self._lock:
            now = time.monotonic(); self._stamp = [t for t in self._stamp if now - t < 60.0]
            if len(self._stamp) >= 10:
                await asyncio.sleep(60.0 - (now - self._stamp[0]) + 0.1)
            self._stamp.append(time.monotonic())

    async def ainvoke(self, prompt, **kw):
        async with self._csem:
            await self._throttle()
            return await self._inner.ainvoke(prompt, **kw)
    async def ainvoke_messages(self, messages, **kw):
        async with self._csem:
            await self._throttle()
            return await self._inner.ainvoke_messages(messages, **kw)
    async def abatch_invoke(self, prompts, *, max_concurrency=None, **kw):
        # Ignore per-call cap; the shared semaphore is the single global gate.
        return await self._inner.abatch_invoke(prompts, max_concurrency=self._csem._value and
            None, **kw) if False else await self._serial(prompts, **kw)
    async def _serial(self, prompts, **kw):
        out = []
        for i, p in enumerate(prompts):
            out.append(await self.ainvoke(p, **kw))   # each already throttled
        return out
```
(Sketch — inject `llm=RateLimitedLLM(...)` into `GraphExtraction`,
`ContextualChunking`, and `GraphRAG`; its `abatch_invoke` serializes/throttles
so no call site exceeds the global cap. `aiolimiter.AsyncLimiter(rate=10,
capacity=10)` + `Semaphore(2)` is a cleaner drop-in than the hand-rolled
bucket. Confidence: MED — pattern sound but needs a 1-chunk dry run;
`LLMInterface` is an ABC requiring concrete `invoke`/`ainvoke` overrides.)

**Scope.** The 10/60s cap is assumed on the **chat deployment**
(`gpt-5.4-mini`); `text-embedding-3-large` is separate, so the embedder stays
unwrapped (confidence: MED). `abatch_invoke` already retries with backoff
(`max_retries=3`), so the limiter keeps us *under* the cap rather than
retry-storming it.

## 4. Assumptions / open items (confidence)

- **G-1 `LiteLLM(model, timeout=8192, num_ctx=...)` signature drift** (HIGH):
  installed `LiteLLM.__init__(model, *, api_key, api_base, api_version,
  temperature=0.0, max_tokens=None, **kwargs)` (litellm.py:37). `timeout` and
  `num_ctx` are **not typed params** — they land in `**self._extra` and are
  forwarded to *every* `litellm.completion(...)`. `timeout=1800` is a valid
  litellm kwarg (desired per-call cap — kept). **`num_ctx` is Ollama-specific**;
  forwarding it to `azure/gpt-5.4-mini` may be ignored or raise. The brief's
  "known signature" is caller intent, not the real constructor.
- **G-2 `gpt-5.4-mini` triggers the reasoning-model path** (HIGH):
  `_is_reasoning_model` matches `gpt-5*`, so LiteLLM strips `temperature` and
  uses `max_completion_tokens` (not `max_tokens`). Setting `num_ctx`/context is
  a *deployment* config, not a per-call knob for Azure. **Recommendation:** do
  **not** pass `num_ctx` for Azure; rely on the deployment's context window, or
  cap output via `max_completion_tokens`. Confidence in the model string itself:
  LOW — `gpt-5.4-mini` must exist on the deployment; unverified.
- **G-3 default chunker discards structure** (HIGH): SDK default
  `SentenceTokenCapChunking` ignores `document.elements`; explicit
  `StructuralChunking` is mandatory for the breadcrumb lever.
- **G-4 `.md` auto-detects to `MarkdownLoader`** (HIGH, main.py:1765) so
  `loader=` is optional but recommended.
- **G-5 `GraphRAG.embedding_dimension=1536` must equal
  `LiteLLMEmbedder(dimensions=1536)`** (HIGH); mismatches raise at
  config-validation.
- **G-6 `entity_types=` on `GraphExtraction` is redundant** when a non-empty
  `ontology=` is on `GraphRAG` (extract() overrides it, graph_extraction.py) —
  harmless, can omit.
- **G-7 rate-limit scope = chat deployment, not embedding** (MED).
- **G-8 `RateLimitedLLM` sketch untested** (MED) — validate with a 1-chunk dry
  run before a full ingest; consider `aiolimiter` + `Semaphore(2)` over the
  hand-rolled token bucket.
- **G-9 `markdown-it-py` present (4.2.0), `graphrag-sdk[markdown]` dep
  satisfied** (HIGH) — `MarkdownLoader` won't raise ImportError.
