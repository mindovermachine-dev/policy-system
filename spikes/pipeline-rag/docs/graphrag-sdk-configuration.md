# © 2026 Cartman ApS. All rights reserved.
# GraphRAG-SDK configuration reference

**Purpose:** catalog everything in GraphRAG-SDK that can be configured or
extended, and answer — informed by that catalog plus this spike's own
`LEARNINGS.md` — whether to keep using it, and if so, in what order to try
things next.

## Methodology note — why this is sourced from installed code, not the docs site

The task behind this doc was to read
[docs.falkordb.com/genai-tools/graphrag-sdk](https://docs.falkordb.com/genai-tools/graphrag-sdk)
in full. That page links out to GitHub markdown docs
(`getting-started.md`, `architecture.md`, `configuration.md`,
`strategies.md`, `providers.md`, `benchmark.md`, `api-reference.md`). Fetching
those turned up a direct contradiction: the fetched `strategies.md` asserted
GraphRAG-SDK has only two entity-resolution strategies ("no
`SemanticResolution` or `LLMVerifiedResolution`") — but `LEARNINGS.md`
describes this spike actually running both against real data.

Rather than build a reference doc on a source caught being wrong, this
switched to the installed package as ground truth:

```
$ pip show graphrag-sdk
Version: 1.4.0
Location: .../lib/python3.11/site-packages
```

Every claim below is read directly from
`graphrag_sdk` 1.4.0's source in that location, not from the doc site's
prose. Where the doc site and the source agreed (e.g. the 9-step pipeline
shape, default chunk size), that's noted as corroboration. `requirements.txt`
pins `graphrag-sdk[litellm,pdf]` with no version floor, so a future
`pip install -U` could change any of this — re-check against the installed
version before trusting this doc long after 2026-08-14.

---

## 1. Pipeline architecture

`IngestionPipeline` (`ingestion/pipeline.py`) runs a fixed sequence, not a
generic DAG — this is a deliberate design choice ("debuggable, loggable,
understandable" per the source docstring), not a limitation:

| # | Step | Configurable? | Strategy ABC |
|---|------|---|---|
| 1 | Load | Yes | `LoaderStrategy` |
| 2 | Chunk | Yes | `ChunkingStrategy` |
| 3 | Lexical graph (Document/Chunk nodes, `PART_OF`/`NEXT_CHUNK`) | No — mandatory | — |
| 4 | Extract (entities + relationships) | Yes | `ExtractionStrategy` |
| 4b | Quality filter (drop empty-id/label nodes) | No — mandatory | — |
| 5 | Prune (filter against ontology) | No — mandatory, but *behavior* depends on the ontology you supply | — |
| 6 | Resolve (deduplicate entities) | Yes | `ResolutionStrategy` |
| 7 | Write (batched upsert to FalkorDB) | No | — |
| 8 | Mentions (`MENTIONED_IN` edges) | No | — |
| 9 | Index chunks (embed + vector index) | No | — |

Steps 8–9 run in parallel with each other; everything else is sequential.
`GraphRAG.ingest()` picks sensible defaults for every strategy you don't
supply explicitly (see §2 per stage). After all sources are ingested,
`GraphRAG.finalize()` runs **cross-document** dedup + embedding backfill +
indexing — skipping it "leaves cross-document duplicates in place and
disables entity-/edge-level vector search" (source docstring, verbatim).
`ingest.py` already calls this.

---

## 2. Full configuration surface, by stage

### 2.1 Ontology (`Ontology`, `Entity`, `Relation`, `Attribute`)

⚠️ **Deprecation note, not a bug in this spike**: `GraphSchema`/`EntityType`/
`RelationType`/`PropertyType` are deprecated aliases as of SDK v1.2+ (they
still work, each access emits `DeprecationWarning`). `schema.py` already
imports the current names (`Entity, Ontology, Relation` from `graphrag_sdk`)
— nothing to change there.

- `Ontology(entities=[...], relations=[...])` — top-level schema.
- `Entity(label, description, properties=[Attribute(...)])`.
- `Relation(label, description, patterns=[(src_label, tgt_label), ...], properties=[...])`.
  **Direction matters and is exactly what `schema.py` already encodes**:
  `patterns=[("Regulation", "Role")]` means the arrow only ever runs
  `(Regulation)-[REL]->(Role)`.
- `Attribute(name, type, description)` — `type` is one of `STRING, INTEGER,
  FLOAT, BOOLEAN, DATE, LIST`.
- Reserved property names (`name, description, source_chunk_ids, spans,
  rel_type, fact, src_name, tgt_name, id, label`) are SDK-managed — declaring
  an `Attribute` with one of these shadows the system value. `Ontology`
  warns (not errors) if a `Relation.patterns` label isn't declared in
  `entities` — a free typo-catcher this spike's `schema.py` already passes
  cleanly (every pattern label is a declared `Entity`).
- `Ontology.from_sources(...)` — LLM- or catalog-driven **auto-discovery** of
  an ontology from a document corpus. Not relevant here — this spike already
  has a fixed target ontology (`ps-domain-concepts.md`) — but worth knowing
  it exists for a future "does GraphRAG-SDK's own idea of this domain's
  schema differ from ours" sanity check.
- `Ontology.merge()` / `.save_to_file()` / `.from_file()` — schema-as-JSON
  round-trip.
- Runtime ontology evolution: `GraphRAG.add_entity/add_attribute/
  rename_entity/rename_attribute/drop_entity/...` — mutate the ontology
  *and* migrate already-ingested data to match, without a full re-ingest.
  Not relevant to this spike's current phase (still establishing whether
  the fixed ontology extracts correctly at all) but useful later if e.g. a
  new `Relation` pattern needs adding after CRA is already ingested.

### 2.2 Loaders (`LoaderStrategy`)

| Loader | Populates `document.elements`? | Notes |
|---|---|---|
| `TextLoader(encoding="utf-8")` | No | Plain text passthrough. |
| `PdfLoader()` | **No** | Prefers PyMuPDF (`pip install graphrag-sdk[pdf-fast]`, AGPL-3.0) for layout-preserving extraction, falls back to `pypdf` (`[pdf]`, Apache-2.0, what `requirements.txt` currently installs). Text only — no structural metadata. |
| `MarkdownLoader(encoding="utf-8")` | **Yes** | Parses headers/paragraphs into a `DocumentElement` tree with `breadcrumbs` (the header hierarchy each paragraph sits under). |

**This matters concretely for this spike**: `docs/regulations/` already has
both `CRA.pdf`/`CRA.md` (and the NIS2/GDPR pair). `ingest.py` currently
ingests the `.pdf` files, which means `document.elements` is always empty —
`StructuralChunking` (§2.3) is silently unusable against the current input
choice, not because it doesn't work, but because the loader in use never
populates what it needs.

### 2.3 Chunking strategies (`ChunkingStrategy`)

Default when `ingest()` isn't given a `chunker=`:
**`SentenceTokenCapChunking(max_tokens=512, overlap_sentences=2)`** — this
is also GraphRAG-Bench's benchmark configuration, corroborated by both the
doc site and source defaults agreeing.

| Strategy | Needs | What it does |
|---|---|---|
| `SentenceTokenCapChunking(max_tokens=512, overlap_sentences=2, encoding_name="cl100k_base")` | tiktoken only | Sentence-boundary splitting, greedy-packed to a token cap, N-sentence overlap. Default. |
| `FixedSizeChunking(chunk_size=1000, chunk_overlap=100)` | — | Character-window splitting. Benchmark doc mentions `chunk_size=1500, chunk_overlap=200` as a "richer context, more tokens" variant — untested here. |
| `StructuralChunking(max_tokens=512, fallback_chunker=...)` | **`document.elements`** (i.e. `MarkdownLoader`, not `PdfLoader`) | Groups elements by structure, keeping paragraphs with their header breadcrumbs; oversized elements fall back to `SentenceTokenCapChunking` by default. |
| `ContextualChunking(base_chunker, llm)` | 1 extra LLM call **per chunk** | Anthropic-style contextual retrieval: LLM writes a 1–2 sentence "where this chunk sits in the document" blurb, prepended to the chunk text before it's stored/embedded/**extracted from**. |
| `CallableChunking(fn)` | — | Adapter for any external chunking function (LlamaIndex, LangChain, spaCy, custom regex, etc.), sync or async. |

### 2.4 Extraction strategy (`ExtractionStrategy` — `GraphExtraction`)

`GraphExtraction(llm, *, entity_extractor=None, coref_resolver=None,
entity_types=None, max_concurrency=None)` runs a **two-step** process per
chunk:

- **Step 1 — entity extraction**, via a pluggable `EntityExtractor`.
  **Default: `GLiNERExtractor(threshold=0.75, model_name="urchade/gliner_medium-v2.1")`**
  — a local, non-LLM, zero-shot NER transformer. It never calls the chat
  LLM. Alternative: `LLMExtractor(llm, threshold=0.75)` — uses the same LLM
  passed to `GraphExtraction`, via `NER_PROMPT`, and (when driven through
  `GraphExtraction.extract()`, not called standalone) receives the full
  per-type descriptions from the ontology, same as Step 2.
- **Step 2 — LLM verify + relationship extraction**, always via the
  supplied `llm`. Receives Step 1's entity list, the full ontology (type
  descriptions, declared relation patterns with direction, any declared
  attributes), and the chunk text; returns verified/corrected entities plus
  all extracted relationships, batched across chunks via `llm.abatch_invoke`.
- `entity_types` — overridden by `ontology.entities` whenever the ontology
  is non-empty (this spike's case) — the constructor arg only matters for
  an open/schema-less ontology.
- `coref_resolver` — optional pre-pass that resolves pronouns to their
  referents before extraction. Not currently used by `ingest.py`.
- `max_concurrency` — this is the **only** thing `ingest.py`'s
  `--max-concurrency` flag actually threads into (confirmed by `ingest.py`'s
  own comment, and confirmed again reading `GraphExtraction.extract()`
  directly: it's passed to `abatch_invoke(..., max_concurrency=...)` for
  both Step 1-via-LLMExtractor and Step 2).

**⚠️ New finding, not in `LEARNINGS.md`**: this spike's `ingest.py` never
sets `entity_extractor=`, so every run to date — Ollama and Azure alike —
did Step 1 with **GLiNER**, not with the chat LLM. GLiNER is a zero-shot
span-tagger built for concrete, name-like entities (its own benchmark
vocabulary is Person/Organization/Location-shaped). This ontology's abstract
classification types — `PracticeArea`, `RiskPath`, `Capability`,
`Obligation` — are not proper-noun spans in the source text the way
"Manufacturer" or "CRA" are; they're categories the model has to *infer*,
which is closer to what Step 2's LLM already does well (per `schema.py`'s
rich descriptions) than what a generic NER model is built for. This has
never been isolated as a variable — every pruning/hub-skipping number in
`LEARNINGS.md` is downstream of whatever GLiNER handed to Step 2 as
"pre-extracted entities," and Step 2's prompt explicitly treats that list as
something to "verify" (bias toward keeping) rather than build from scratch.
Swapping to `entity_extractor=LLMExtractor(llm)` is untested and cheap to
try — cost is one extra LLM call per chunk (see the concurrency note below
before combining this with Azure's rate limit).

### 2.5 Pruning (built-in — not a strategy)

`IngestionPipeline._prune()` filters Step 4's output against
`ontology.entities` / `ontology.relations` — this is the mechanism behind
`LEARNINGS.md`'s 71%-pruned number. Two things worth knowing that
`LEARNINGS.md` doesn't call out:

- It **already logs a structured warning** naming the offending
  `(src_label, tgt_label)` pairs per relation type, with the note *"If
  extraction looks correct, the pattern direction may be inverted"* — this
  is exactly the diagnostic `LEARNINGS.md` built by hand (grepping for
  `HAS`/`SATISFIED_BY`/etc. pruning counts). It's a standard library
  `logger.warning` call, so as long as `logging.basicConfig(level=INFO)` is
  set (it is, in `ingest.py`) it should already be visible without any
  custom code — worth confirming the next full run's logs actually surface
  it, rather than re-deriving pruning counts manually.
- Node pruning explicitly **keeps** any node labeled `"Unknown"` (Step 1/2's
  fallback label for a type the model couldn't map to a declared entity
  type). These survive into the graph as `Unknown`-labeled nodes rather than
  being dropped — worth checking whether `compare.py`'s structural-parity
  counts are currently including or excluding these.

### 2.6 Resolution strategies (`ResolutionStrategy`)

Default when `ingest()` isn't given a `resolver=`: **`ExactMatchResolution()`**.

| Strategy | LLM/embedder needed? | Merge key | Defaults |
|---|---|---|---|
| `ExactMatchResolution(resolve_property="id")` | No | `(label, resolve_property value)` | With the default `resolve_property="id"`, survivors and "duplicates" already share an id — merges are a no-op in practice. Passing `resolve_property="name"` would make this a genuine same-name-collapses-to-one dedup, still with zero LLM cost. |
| `DescriptionMergeResolution(llm=None, force_summary_threshold=3, max_summary_tokens=500)` | LLM only above threshold | `(normalized_name, label)` | Below threshold: descriptions concatenated with `" \| "`. At/above: one LLM summarization call per group. |
| `SemanticResolution(llm=None, embedder=None, similarity_threshold=0.95, force_summary_threshold=3, max_summary_tokens=500, ann_top_k=50)` | Embedder for fuzzy phase | Phase 1: `(normalized_name, label)` exact. Phase 2 (if `embedder` given): cosine similarity of **name-only** embeddings via hnswlib ANN, within same-label groups. | `similarity_threshold=0.95` default, docstring literally says **"very conservative."** |
| `LLMVerifiedResolution(llm=None, embedder=None, hard_threshold=0.95, soft_threshold=0.80, max_llm_pairs=500, max_llm_concurrency=None, ...)` | Both | Phase 1 exact, then: `sim ≥ 0.95` auto-merge, `0.80 ≤ sim < 0.95` → LLM YES/NO verification (agglomerative-clustered first to cut LLM calls), `sim < 0.80` skipped entirely. | `max_llm_concurrency=None` → falls through to the LLM instance's own `max_concurrency` (see §2.7 — this is **12 by default and not the same knob as `ingest.py`'s `--max-concurrency`**). |

**Cross-reference to `LEARNINGS.md`'s two "did NOT clearly work" resolver
findings**:
- *"`SemanticResolution` did not merge a known duplicate pair even at a
  lowered threshold"* — Phase 2's fuzzy merge only runs on **name
  embeddings**, not descriptions, and only within nodes that already
  survived Phase 1 as separate entries (i.e. genuinely different normalized
  names). `0.95` is already the conservative end of cosine similarity for
  short strings; "lowered" isn't stated to what value in `LEARNINGS.md` —
  worth re-testing explicitly down to e.g. `0.80–0.85` before concluding the
  strategy itself doesn't work, since the default is documented as
  deliberately strict.
- *"`LLMVerifiedResolution` gave an ambiguous result, confounded by its own
  uncontrolled concurrency"* — **confirmed at the code level, not just
  suspected**: `max_llm_concurrency` defaults to `None`, and when `None`,
  `abatch_invoke` falls back to the LLM provider instance's own
  `max_concurrency` attribute — which is **always 12** for any `LiteLLM(...)`
  instance in this SDK version (see §2.7). `ingest.py`'s `--max-concurrency`
  flag never touches this. Any resolver-driven LLM verification batch will
  run at concurrency 12 regardless of what `--max-concurrency` says, unless
  `max_llm_concurrency=` is passed explicitly to `LLMVerifiedResolution(...)`
  or `llm.max_concurrency` is mutated directly after construction (it's a
  plain public attribute, not read-only).

### 2.7 LLM/Embedder providers (`LiteLLM`, `LiteLLMEmbedder`, `OpenRouterLLM`/`Embedder`, custom)

- `LiteLLM(model, *, api_key=None, api_base=None, api_version=None,
  temperature=0.0, max_tokens=None, **kwargs)`. Everything in `**kwargs`
  (e.g. this spike's `timeout=` and `num_ctx=`) is stored and merged into
  every `litellm.completion(...)`/`acompletion(...)` call verbatim — this is
  exactly the mechanism `ingest.py`'s own docstring already correctly
  documents for why an explicit `timeout` was needed against Ollama.
- **Reasoning-model handling is automatic**: `LiteLLM._is_reasoning_model()`
  detects `o1`/`o3`/`gpt-5*` by name and strips `temperature`, translates
  `max_tokens` → `max_completion_tokens`. Relevant if a future run points at
  a bare `gpt-5`/`o-series` deployment instead of `gpt-5.4-mini` (verify
  `gpt-5.4-mini` itself doesn't trip this path unexpectedly, since the
  detection is a name-prefix match).
- **⚠️ Concurrency footgun, new finding**: `LiteLLM.__init__` does **not**
  accept or forward a `max_concurrency` parameter to its parent
  `LLMInterface.__init__`. Every `LiteLLM(...)` instance therefore has
  `max_concurrency == 12` (the `LLMInterface` base default) — always,
  regardless of what's passed to the constructor. Two consequences:
  - Passing `max_concurrency=N` into `LiteLLM(...)` does **not** set the
    concurrency limit — it silently lands in `**kwargs` → `self._extra` →
    gets forwarded as a literal parameter on every `litellm.completion()`
    call, which most providers will either ignore or error on.
  - The only ways to actually change an `LLMInterface`'s own concurrency
    are: (a) pass `max_concurrency=` explicitly to a call that accepts it
    per-call (`abatch_invoke(..., max_concurrency=...)`,
    `GraphExtraction(..., max_concurrency=...)`,
    `LLMVerifiedResolution(..., max_llm_concurrency=...)`), or (b) mutate
    the instance attribute directly after construction: `llm.max_concurrency
    = 3`. `ingest.py` currently only does (a), and only for the extraction
    step — anything that calls `llm.abatch_invoke()` without an explicit
    override (both fuzzy resolvers, `DescriptionMergeResolution`'s summary
    batching, `BackfillExecutor`) runs at the hardcoded 12.
- `LiteLLMEmbedder(model, *, api_key=None, api_base=None, api_version=None,
  batch_size=2048, **kwargs)` — same passthrough-kwargs pattern.
  `batch_size` splits `embed_documents`/`aembed_documents` calls; Azure
  embedding endpoints commonly cap batch size lower than the 2048 default
  (this spike's `ingest.py` doesn't currently override it — worth checking
  against whatever the actual Azure embedding deployment's limit is before
  a full multi-source run).
- `OpenRouterLLM`/`OpenRouterEmbedder` — same shape, OpenRouter-specific.
  Not used by this spike; only relevant if cost-comparing providers becomes
  a goal later.
- Custom providers: subclass `LLMInterface` (implement `invoke()`) or
  `Embedder` (implement `embed_query()` + `model_name`). Override
  `embed_documents()` for real batch performance — the ABC default is a
  sequential `embed_query()` loop per text.

### 2.8 Context (`Context`, `latency_budget_ms`)

`Context(tenant_id="default", latency_budget_ms=None)` is threaded through
every strategy call. When `latency_budget_ms` is set, `provider_timeout_seconds()`
raises `LatencyBudgetExceededError` once the budget is exhausted, and
`GraphExtraction.extract()` uses `ctx.budget_exceeded` to stop pulling in new
chunks mid-run. `ingest.py` never constructs a `Context` with a budget — this
is the exact mechanism behind the Ollama timeout incident documented in
`README.md`'s "Model choice" section, and that diagnosis is confirmed
correct at the source level: with no budget set, per-call timeout comes
**only** from whatever's in `LiteLLM`'s own `**kwargs` (the explicit
`timeout=1800` `ingest.py` now passes), never from the SDK itself.

### 2.9 Retrieval / reranking (not yet reached by this spike)

Default: `MultiPathRetrieval(graph_store, vector_store, embedder, llm, *,
chunk_top_k=15, max_entities=30, max_relationships=20, rel_top_k=15,
keyword_limit=10)` — combines vector search, fulltext, and 2-hop graph
traversal, then `CosineReranker(embedder, top_k=15)`. `LocalRetrieval` is a
simpler single-path alternative. Flagged here only for completeness —
comparison dimension 4 (falsification-step compatibility) is the first
point this spike would actually touch retrieval, and that's still "not yet
done" per `LEARNINGS.md`.

### 2.10 Post-ingestion (`finalize()`, `deduplicate_entities()`)

`finalize()` — already called by `ingest.py` — bundles: null-stub cleanup →
`deduplicate_entities()` (exact-name only by default, `fuzzy=False`) →
entity/relationship embedding backfill → index creation. Passing
`fuzzy=True` (with a `similarity_threshold`, default `0.9`) to
`deduplicate_entities()` directly would run the same embedding-based
near-duplicate merge as `SemanticResolution`, but **globally across all
already-ingested documents** rather than per-ingest-call — this is a
different, cheaper entry point to test the cross-regulation `Capability`
convergence question (comparison dimension 3) than re-running ingestion
with a different `resolver=`.

---

## 3. Recommendation

**Keep using GraphRAG-SDK — do not conclude "hand-built pipeline required"
yet.** Every concrete failure mode logged in `LEARNINGS.md` maps to a
specific, named, still-untried configuration lever, not to a structural gap
in what the SDK can do. The clearest sign of this: this spike has been
running Step 1 of every single ingest through a generic NER model
(`GLiNERExtractor`, the default) without that ever being a deliberate
choice — the actual chat LLM the spike is choosing carefully between Ollama
and Azure never sees Step 1 at all.

**Suggested order, cheapest/highest-signal first:**

1. **Swap `entity_extractor=LLMExtractor(llm)` into `GraphExtraction` in
   `ingest.py`.** Zero new infrastructure, one line, and it's the single
   biggest untested variable — GLiNER was never a deliberate choice for
   this domain's abstract entity types. Re-run the same fixed 14.5KB CRA
   excerpt `LEARNINGS.md` already uses for A/B comparability against the
   existing 38-pruned baseline. Cost: roughly doubles per-chunk LLM calls
   (Step 1 + Step 2 both hit the LLM now) — on Ollama that's more wall time,
   not more money; on Azure it interacts with the rate-limit problem below,
   so sequence this before any full-corpus Azure run.
2. **Fix the resolver-concurrency footgun** before touching
   `SemanticResolution`/`LLMVerifiedResolution` again: either pass
   `max_llm_concurrency=` explicitly to `LLMVerifiedResolution(...)`, or set
   `llm.max_concurrency = N` on the constructed `LiteLLM` instance right
   after creation. Then re-test `LLMVerifiedResolution` — its result was
   "ambiguous" specifically because this confound was live, not because the
   strategy itself was shown not to work.
3. **Re-test `SemanticResolution` at an explicit, logged lower threshold**
   (e.g. `0.80`) — `LEARNINGS.md` says "lowered" without recording the
   value, and `0.95` is documented in the source as deliberately
   conservative, not a reasonable-default that was already given a fair
   shot.
4. **Switch the regulation source from PDF to the existing `.md` files +
   `MarkdownLoader()` + `StructuralChunking()`.** This was already flagged
   in `README.md` as a "cheap follow-up" for extraction-quality reasons;
   what wasn't known at the time is that it also *unlocks* a chunker
   (`StructuralChunking`) that's currently silently inert against PDF input,
   specifically because `PdfLoader` never populates `document.elements`.
   Header-breadcrumb context per chunk is a plausible, low-cost lever
   against the hub-skipping failure mode (the model would see "this
   paragraph is under Article 13 → Chapter II" rather than a bare text
   window), independent of and complementary to the direction-explicit
   schema wording already in `schema.py`.
5. **Only after 1–4**, revisit the still-open "anti-shortcut description"
   question from `LEARNINGS.md` (§ "Untested variables" #1) — isolating
   `Regulation`-only vs `Role`-only changes. It's still a real open
   question, just lower-signal than the four above since it's iterating on
   a variable (prompt wording) the SDK already exposes cleanly, rather than
   uncovering a variable that was silently defaulted the whole time.

**What would change this recommendation**: if step 1 (LLM-based Step-1
extraction) does *not* meaningfully move the 71%-pruned baseline, that's
real evidence the problem is Step 2's relationship-extraction prompt
compliance specifically, not entity quality feeding into it — at that point
the SDK's own exposed levers (schema wording, chunking, resolvers) have
been substantively exhausted, and the case for the current comparison
dimensions (structural parity, content fidelity, convergence, falsification
compatibility) shifts toward finishing them as-is rather than continuing to
tune extraction.
