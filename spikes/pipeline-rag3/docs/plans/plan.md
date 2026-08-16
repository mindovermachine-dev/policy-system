# Implementation Plan — pipeline-rag3 (ACs)

**Target:** `spikes/pipeline-rag3/docs/plans/plan.md`  
**Agent:** `qwen3-coder-next:q4_K_M` (coding), `qwen2.5-coder:14b` (design/RCA)  
**Model choice rationale:** `qwen3-coder-next` is optimized for Python code generation and debugging; `qwen2.5-coder:14b` is reserved for strategy/RCA where reasoning depth matters. Avoid same-model contention per `LEARNINGS.md` E1.

---

## Part A — Discovery

**Goal:** Get `AZURE_API_KEY`, `AZURE_API_BASE`, `AZURE_API_VERSION` verified before ingestion.

### A.1 — Fetch Azure API Key

| Step | Action | Expected Output | Exit Criterion |
|------|--------|-----------------|----------------|
| A.1.1 | Run: <br>`az cognitiveservices account keys list --name policy-system-graphrag-spike --resource-group rg-policy-system-graphrag-spike --query key1 -o tsv` | Pure 64-char hexadecimal key (no quotes/newline) | `echo $AZURE_API_KEY` yields non-empty string |
| A.1.2 | Store in `./api_key.txt`: <br>`az cognitiveservices... > api_key.txt 2>/dev/null` | `api_key.txt` exists, 64-char hex | File readable, non-empty |

*Sub-agent:* `qwen3-coder-next:q4_K_M`  
*Command:* `aws` (no pi agent needed)  
*Reference:* `ingest.py` L57-66 `_require_azure_env()` demands these three vars.

### A.2 — Derive & Verify Azure Base URL

| Step | Action | Expected Output | Exit Criterion |
|------|--------|-----------------|----------------|
| A.2.1 | Construct base = `https://policy-system-graphrag-spike.openai.azure.com/openai/deployments/` | String matches Azure resource naming | No typos, correct region |
| A.2.2 | Construct API version = `2024-10-21` (latest for `gpt-5.4-mini` perlitellm docs) | Version string for `api_version=` | `litellm.completion(..., api_version="2024-10-21")` succeeds |

*Sub-agent:* `qwen3-coder-next:q4_K_M`  
*Command:* Derive via pattern match (no external call needed)  
*Reference:* `LEARNINGS.md` D6 confirms API version determination at first dry-run.

### A.3 — Verify Credentials (1-chunk dry run)

| Step | Action | Expected Output | Exit Criterion |
|------|--------|-----------------|----------------|
| A.3.1 | Set env: <br>`export AZURE_API_KEY=<from A.1.2> AZURE_API_BASE=https://policy-system-graphrag-spike.openai.azure.com/openai/deployments/ AZURE_API_VERSION=2024-10-21` | No errors | Shell variables bound |
| A.3.2 | Run: <br>`python ingest.py --source cra --reset --max-chunks 1` | Exits 0, 1 chunk, no Azure errors | `logs/ingest-*.jsonl` contains `event:source_done` |

*Sub-agent:* `qwen3-coder-next:q4_K_M`  
*Command:* Run `ingest.py` per brief  
*Reference:* `ingest.py` L207 `CappedChunker` caps chunks to 1 when `--max-chunks 1`.

---

## Part B — 1-chunk Dry Run

**Goal:** Prove mechanism end-to-end before full ingestion.

### B.1 — Run Dry Run Command

| Step | Action | Expected Output | Exit Criterion |
|------|--------|-----------------|----------------|
| B.1.1 | `python ingest.py --source cra --reset --max-chunks 1` | Exits 0, 1 chunk extracted, graph populated with minimal nodes | Graph has >0 nodes in `policy_system_graphrag_native` |
| B.1.2 | Inspect `logs/ingest-*.jsonl` | Events: `start`, `source_done`, `finalize_done`, `summary` | JSONL contains all required events |

*Sub-agent:* `qwen3-coder-next:q4_K_M`  
*Command:* Execute ingestion per `run_worker.sh` harness (see Part D for timeout)  
*Reference:* `ingest.py` L207 uses `CappedChunker` to limit to 1 chunk.

### B.2 — Semantic Logging Expectations

| Event | Fields | Purpose |
|-------|--------|---------|
| `start` | `graph, sources, model, embed, chunker` | Confirm configuration resolved correctly |
| `source_done` | `nodes_created, relationships_created, wall_s` | Confirm extraction produced data |
| `finalize_done` | — | Confirm cross-document dedup/embed completed |
| `summary` | `total_s, sources_processed, errors_count` | Final timing and error count |

*Reference:* `ingest.py` L125-129 emits these via `emit()`.

### B.3 — Exit Criteria

| Outcome | Action |
|---------|--------|
| A.3 passes (1-chunk succeeds) | Proceed to Part C (scale-up decision) |
| A.3 fails (exits non-zero) | RCA: check `logs/ingest-*.err`, `api_key.txt`, network, ollama health |

*Sub-agent:* `qwen3-coder-next:q4_K_M` ( RCA) → `qwen2.5-coder:14b` if RCA reveals design fix needed

---

## Part C — Scale-Up Decision

**Goal:** Determine chunk count for "non-trivial graph" without overspending.

### C.1 — Chunk Count Recommendation

| Metric | Value | Rationale |
|--------|-------|-----------|
| Recommended max chunks | `--substantive 15 --filter-regex /shall/i --spread` | CRA body (pp. 31–70) contains `shall`; 15 substantive chunks = ~50–75 substantive extraction targets |

*Sub-agent:* `qwen2.5-coder:14b` (design)  
*Reference:* `LEARNINGS.md` I6: "FilteringChunker `--substantive N --filter-regex /shall/i` reaches CRA body (shall concentrated pages 31–70)."

### C.2 — Wall Time Estimates

| Run | Estimated time | Watchdog timeout |
|-----|----------------|------------------|
| 1-chunk dry run | ~1 min | 120s (current harness default) |
| Full run (15 substantive chunks) | ~3-5 min | 600s (10 min) |

*Rationale:* LLM calls ~15-30s each; 15 chunks × 2 LLM calls (Step 1 + Step 2) × concurrency=2 → ~15-30 min raw, but rate limiting (10 req/60s) may extend this. Per harness `run_worker.sh` default is 300s; increase to 600s for full run.

*Sub-agent:* `qwen3-coder-next:q4_K_M` (timing estimate)  
*Reference:* `ingest.py` L203 `RateLimitedLLM(llm_raw, concurrency=2, req_per_window=10, window_s=60.0)`.

### C.3 — Full Run Command

| Step | Action | Expected Output | Exit Criterion |
|------|--------|-----------------|----------------|
| C.3.1 | `python ingest.py --source cra --reset --substantive 15 --filter-regex /shall/i --spread` | Exits 0, graph populated with meaningful structure | Graph has >50 nodes, >100 edges |

*Sub-agent:* `qwen3-coder-next:q4_K_M`  
*Reference:* `ingest.py` L207-212 `FilteringChunker` uses regex match + spread selection.

---

## Part D — compare.py Execution

**Goal:** Verify graph structure after ingestion.

### D.1 — Run compare.py

| Step | Action | Expected Output | Exit Criterion |
|------|--------|-----------------|----------------|
| D.1.1 | `python compare.py` | Two reports: `BASELINE (policy_system)` and `GRAPHRAG-SDK (policy_system_graphrag_native)` | Exit 0, no exceptions |
| D.1.2 | Compare node/edge vocabularies | Baseline: 776n/1475e; GRAPHRAG: [n]<sub>node</sub>, [e]<sub>rel_type</sub> | No AttributeError, labels match expected schema |

*Sub-agent:* `qwen3-coder-next:q4_K_M`  
*Command:* `python compare.py --graphrag-graph policy_system_graphrag_native --baseline-graph policy_system`  
*Reference:* `compare.py` L96-120 `report()` prints counts per `EXPECTED_LABELS`/`EXPECTED_EDGE_TYPES`.

### D.2 — Acceptable Structural Findings

| Metric | Good-enough indicator | Action |
|--------|----------------------|--------|
| Node labels present | All 10 expected labels exist (even if counts vary) | Proceed to Part E |
| Edge types present | At least 5 of 11 expected edge types > 0 | Proceed to Part E |
| Unexpected labels | Zero (or rare `Unknown` fallback) | Proceed; `Unknown` is expected fallback |

*Reference:* `compare.py` L32-57 `node_counts()` gates on `__Entity__` marker.

---

## Part E — Iteration

**Goal:** Failure recovery, logging, and decision making.

### E.1 — Error Taxonomy & Recovery

| Error | Diagnosis | Recovery |
|-------|-----------|----------|
| `AZURE_API_KEY` not set | Env not bound | Re-run `az cognitiveservices... | tee api_key.txt` |
| Connection refused (FalkorDB) | DB not on :6379 or wrong host | `falkordb.FalkorDB(host=localhost,port=6379).list_graphs()` |
| Extraction timeout | LLM call >1800s | Increase `timeout=1800` in `ingest.py` L145 → 3600 |
| Rate limit hit | >10 req/60s | Reduce `--max-concurrency` or wait between runs |

*Sub-agent:* `qwen2.5-coder:14b` (RCA)

### E.2 — Partial Run Recovery

| Scenario | Recovery |
|----------|----------|
| Full run fails mid-way | `--reset` + re-run (graph is safe to delete per `ingest.py` L107 `reset_graph()`) |
| Ingestion succeeds but compare.py fails | Check graph name (`policy_system_graphrag_native` vs `pipeline-rag3_graphrag_native` typo); verify node `__Entity__` labels (schema mapping issue) |

*Reference:* `ingest.py` L93-98 `reset_graph()` uses `graph.delete()` — scoped to target only.

### E.3 — Go/No-Go Decision

| Condition | Action |
|-----------|--------|
| AC1 met (non-trivial graph) + AC2 runs without errors | Proceed to pipeline-rag4 design + manual inspection |
| AC1 fails (empty graph or minimal nodes) | RCA, fix root cause, retry up to 2×; if still fails after 3× → pivot to pr3 redesign |

*Sub-agent:* `qwen2.5-coder:14b` (strategy decision)

---

## Output Artifacts

| File | When created | Owner |
|------|--------------|-------|
| `logs/ingest-*.jsonl` | Per ingestion run | `ingest.py` L125-129 |
| `logs/ingest-*.out` | Per `run_worker.sh` run | `run_worker.sh` L58 |
| `logs/ingest-*.err` | Per error run | `run_worker.sh` L59 |
| `api_key.txt` | Part A.1.2 | Leader script |
| `docs/plans/plan.md` | This deliverable | Agent |

---

## Summary

| Part | Key Command | Model | Time Budget |
|------|-------------|-------|-------------|
| A.3 (dry run) | `python ingest.py --source cra --reset --max-chunks 1` | qwen3-coder-next:q4_K_M | 120s |
| C.3 (full run) | `python ingest.py --source cra --reset --substantive 15 --filter-regex /shall/i --spread` | qwen3-coder-next:q4_K_M | 600s |
| D.1 (compare) | `python compare.py` | qwen3-coder-next:q4_K_M | 10s |

**Total estimated budget:** 15 min (excluding RCA iterations).
