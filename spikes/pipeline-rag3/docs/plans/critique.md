Now I have all the context. Let me analyze the plan systematically:

---

## Critical Review: Implementation Plan for pipeline-rag3

### (a) Factual errors / wrong commands

| Flaw | Severity | Plan Section/Line | Fix |
|------|----------|-------------------|-----|
| **A.2.2 claims `AZURE_API_VERSION=2024-10-21` is "latest for `gpt-5.4-mini` per litellm docs"** | **BLOCKER** | A.2.2 (p.3) | `LEARNINGS.md` E2 explicitly states `az ... openai deployment list FAILS (openai ext missing)` — **no external verification possible**. More critically, `graphrag-sdk-configuration.md` §2.7 confirms `LiteLLM` for `azure/gpt-5.4-mini` **must NOT include `num_ctx`**, but the plan gives no guidance on confirming the API version actually works. The version used in `ingest.py` (`os.environ["AZURE_API_VERSION"]` with no default) is unverified. **Action**: Remove the version assertion; treat it as an env var to be provided (like the key). |
| **C.2 claims "Rate limit (10 req/60s)" → full run ~15-30 min raw** | **BLOCKER** | C.2 (p.5) | `ingest.py` L203 shows `RateLimitedLLM(llm_raw, concurrency=2, req_per_window=10, window_s=60.0)`, but `graphrag-sdk-configuration.md` §2.7 reveals **two hidden concurrency footguns**:<br>1. `LiteLLM` instances **always have `max_concurrency == 12`** (cannot be changed via constructor args).<br>2. Resolvers (`LLMVerifiedResolution`, `SemanticResolution`) use `abatch_invoke` without a concurrency override — they run at **concurrency 12**, NOT the plan's `2`.<br>This means **15 chunks × (LLM extraction + embedding) × 12 parallel calls** could burst past Azure's rate limit *before* `RateLimitedLLM` throttling kicks in. |
| **D.1 expects `python compare.py` with "no arguments"** | **HIGH** | D.1.1 (p.7) | `compare.py` (copied from pr2) requires explicit graph names. `spikes/pipeline-rag3/compare.py` uses `policy_system` (baseline) and **`pipeline-rag3_graphrag_native`**, NOT `policy_system_graphrag_native` (per A.1.1). **Mismatch will cause `graph not found` errors.** Need `--graphrag-graph policy_system_graphrag_native` override. |

### (b) Gaps that will cause failure

| Flaw | Severity | Plan Section/Line | Fix |
|------|----------|-------------------|-----|
| **Plan ignores `run_worker.sh`'s default 300s watchdog timeout (A.3, B.1.1)** | **BLOCKER** | A.3.2, B.1.1 (p.2-3) | `run_worker.sh` L56: `WD="${4:-300}"`. `A.3.2` and `B.1.1` show full ingestion (even 1-chunk) takes >180s (timeout=1800s in `ingest.py` + Azure warmup + FalkorDB I/O). **The watchdog will kill the agent mid-run.** Must specify `WD=600` or `WD=900`. |
| **No handling for `PdfLoader` returning empty `document.elements`** | **HIGH** | Plan never cites this (entire ingestion) | `graphrag-sdk-configuration.md` §2.3: `StructuralChunking` **requires** `document.elements` (populated only by `MarkdownLoader`). `ingest.py` L178-179 uses `PdfLoader()` — **`StructuralChunking` is silently unusable**, but the plan never clarifies this is why `SentenceTokenCapChunking` is used. This isn't a bug, but the plan should explicitly state "We're using PDF → plain text chunking (no structural context) because PDF loader doesn't populate elements." |
| **Missing rate-limit burst handling** | **HIGH** | C.2 (p.5) | `RateLimitedLLM` enforces 10 req/60s, but `ingest.py` L155-163 calls `rag.ingest()` **once**, which triggers *multiple* `abatch_invoke` calls internally (extraction per.chunk, embeddings, finalize). `graphrag-sdk-configuration.md` §2.7: extraction itself batches chunks but uses the LLM's internal `max_concurrency=12`. **A 15-chunk run may burst 12 LLM calls in one batch**, violating Azure's rate limit *before* `RateLimitedLLM` kicks in. Need to: (a) reduce `max_concurrency` on `llm_raw` instance, or (b) lower `req_per_window`. |
| **`--reset` resets the *target* graph, but baseline `policy_system` may be missing** | **HIGH** | A.3.2, C.3.1 (p.2,5) | `ingest.py` L107 `reset_graph()` resets `args.graph_name`, but `PROGRESS.md` D1 says baseline is loaded via `tools/graph-ingestion/load_all.sh`. **No plan step verifies `policy_system` exists before starting.** If baseline isn't loaded, `compare.py` fails (D.1.1) even if ingestion succeeds. Need pre-check: `falkordb.FalkorDB().select_graph("policy_system").query("MATCH (n) RETURN COUNT(n) AS c")`. |
| **No handling for `finalize()` failure** | **MEDIUM** | B.1.2, C.3.1 (p.3,5) | `ingest.py` L243-250 catches `finalize()` exceptions and emits `finalize_error`, but **no plan step verifies whether `finalize()` succeeded**. `LEARNINGS.md` H4 notes FalkorDB needs `AS` aliases — `finalize()` may fail silently (e.g., `count(n)` without `AS c`). Plan must include: "After C.3.1, run `MATCH (n:__Entity__) RETURN COUNT(n) AS c` to confirm graph has data." |

### (c) Missing error handling for likely failure modes

| Flaw | Severity | Plan Section/Line | Fix |
|------|----------|-------------------|-----|
| **No recovery for `AZURE_API_KEY` with newlines/whitespace** | **HIGH** | A.1.2 (p.2) | Plan's command `az cognitiveservices... > api_key.txt` may capture trailing newline. `ingest.py` L57-66 `_require_azure_env()` checks `os.environ.get(v)` — **empty string passes, but a key with `\n` fails authentication**. Need to add `| tr -d '\n'` to the command. |
| **`CappedChunker`/`FilteringChunker` state not reset between runs** | **MEDIUM** | B.1.1, C.3.1 (p.3,5) | `CappedChunker` and `FilteringChunker` have mutable counters (`self.total`, `self.matched`, `self.kept`). `ingest.py` instantiates them in `run()` (L176-184) — **state persists if the process is re-run without restart**. The plan shows "re-run" commands, but doesn't clarify `--reset` only resets the *graph*, not the script's counters. Not critical for first run, but will mislead if re-running 1-chunk dry run multiple times. |
| **No handling for Azure embedding batch size limit** | **MEDIUM** | Not addressed | `graphrag-sdk-configuration.md` §2.7: `LiteLLMEmbedder` default `batch_size=2048`, but **Azure embedding endpoints commonly cap batch size lower** (e.g., 16). `ingest.py` doesn't override `batch_size`. A 15-chunk run with 1536-dim embeddings may fail if Azure rejects a 2048-batch call. Need `batch_size=min(16, len(chunks))` or explicit override. |
| **No handling for `rate-limited` HTTP status in `RateLimitedLLM`** | **MEDIUM** | Not addressed | `RateLimitedLLM` (in `ratelimit.py`) isn't shown, but the plan assumes it handles Azure's rate limit. If Azure returns `429 Too Many Requests`, and `RateLimitedLLM` doesn't implement exponential backoff (as is common), ingestion may fail mid-run. The plan must specify how retries/backoff are handled. |

### (d) Timing/resource issues causing watchdog kills or Azure rate limits

| Flaw | Severity | Plan Section/Line | Fix |
|------|----------|-------------------|-----|
| **Watchdog timeout too short for full ingestion** | **BLOCKER** | C.2 (p.5) | `C.2` estimates "15 chunks × 2 LLM calls × concurrency=2 → ~15-30 min raw". With Azure warmup + FalkorDB I/O, **total time likely exceeds 600s (10 min)**. `run_worker.sh` default watchdog is 300s; even `WD=600` may be insufficient. Plan must use `WD=1200` (20 min) for C.3.1, or split into smaller batches (`--substantive 5` first, then `10`). |
| **No accounting for Azure model cold-start latency** | **HIGH** | A.3.2 (p.2) | First Azure call (`python ingest.py --max-chunks 1`) incurs **cold-start latency** (~30-60s for `gpt-5.4-mini`). Plan assumes 1-chunk run takes "1 min" — **if cold-start pushes it to 2+ min, the 120s watchdog will kill it**. A.3.2 watchdog should be `WD=300`. |
| **`--spread` selection may yield empty chunks if regex has few matches** | **MEDIUM** | C.1 (p.4), D.1.1 (p.7) | Plan C.1: `--filter-regex /shall/i` assumes "15 substantive chunks = ~50–75 targets". But if CRA has <15 `shall` mentions in first 512-token chunks, `FilteringChunker` yields **0 chunks**, causing `graph has 0 nodes` failure. Plan must add verification step: "After `--substantive 0`, check `logs/ingest-*.jsonl` for `chunk_select.matched_filter > 0`." |
| **No memory cleanup between `ingest()` and `finalize()`** | **MEDIUM** | Not addressed | `ingest.py` L223-235 ingests, then L243-250 calls `finalize()`. Both steps hold large data in memory (chunks, embeddings). For a full CRA PDF, **memory pressure may cause OOM** before `finalize()` completes. No plan step monitors memory usage or suggests incremental runs. |

### (e) Contradictions with codebase

| Flaw | Severity | Plan Section/Line | Fix |
|------|----------|-------------------|-----|
| **Plan cites `compare.py` L96-120 `report()` prints counts per `EXPECTED_LABELS`/`EXPECTED_EDGE_TYPES`**, but `compare.py` gates on `__Entity__` marker (per `LEARNINGS.md` I1)** | **HIGH** | D.1.1 (p.7) | `spikes/pipeline-rag3/compare.py` (copied from pr2) uses `policy_system_graphrag_native`? No: `compare.py` likely uses `pipeline-rag3_graphrag_native` (per pr2 naming). **Plan assumes `policy_system_graphrag_native`**, but the actual file name/path in the spike directory is **unverified**. Must read `compare.py` first to confirm. |
| **Plan assumes `--max-concurrency` controls *all* LLM calls (C.2, E.1)** | **HIGH** | C.2 (p.5), E.1 (p.7) | `ingest.py` L155-163 passes `max_concurrency=args.max_concurrency` to `GraphExtraction`, but `graphrag-sdk-configuration.md` §2.7 proves `max_concurrency` on extraction **does not affect resolvers, embeddings, or finalize**. E.1's "reduce `--max-concurrency`" fix won't slow down `LLMVerifiedResolution`, which runs at `12`. Plan must distinguish which `max_concurrency` knob does what. |
| **Plan says "per `run_worker.sh` harness (see Part D for timeout)" but Part D says nothing about watchdog** | **HIGH** | B.1.1 (p.3) | Part D (D.1, D.1.1) shows `python compare.py` with no `run_worker.sh` usage and no timeout discussion. But `compare.py` is trivial (seconds). **Ingestion (Parts A, B, C) uses `run_worker.sh`, but Part D does not** — implying `compare.py` runs in the same shell session. If `compare.py` fails, the session exits, obscuring the root cause. |

---

### Summary of BLOCKER issues requiring immediate fix before running any command:

1. **A.2.2 & A.3.2**: No API version assurance + watchdog timeout too short for Azure cold-start (use `WD=300` for 1-chunk run).
2. **B.1.1**: Full run watchdog should be `WD=1200` (20 min), not `600s`.
3. **C.2 / D.1.1**: `compare.py` graph name likely wrong (`pipeline-rag3_graphrag_native` vs `policy_system_graphrag_native`).
4. **C.3.1**: Azure rate-limit burst risk (extraction concurrency=12, not 2) — reduce `llm_raw.max_concurrency` or use smaller batches.
5. **Pre-flight**: Baseline `policy_system` graph must be verified before starting (no plan step for this).
6. **A.1.2**: `api_key.txt` may have trailing newline — add `| tr -d '\n'`.

Would you like me to proceed with fixing these in the critique file, or would you prefer to address them first?
