<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: Pipeline via GraphRAG-SDK

**Status:** Scaffolded 2026-08-14, not yet run.

## Purpose

Determine whether [FalkorDB's GraphRAG-SDK](https://docs.falkordb.com/genai-tools/graphrag-sdk)
can replace the current bespoke, agent-driven regulation ingestion pipeline
(`spikes/graph-ingestion3` → `tools/graph-ingestion`). This spike ingests
CRA, NIS2, and GDPR (raw PDFs) plus Engineering Practices (a narrative
rewrite of `test-data/engineering-practices/engineering-practices-seed.json`)
through GraphRAG-SDK, constrained to the same `ps-domain-concepts.md` schema
the current pipeline targets, and compares the result against the existing
`policy_system` graph.

## Not a shared import

This spike does not import `tools/graph-ingestion/*` or
`spikes/graph-ingestion3/*` — it writes to its own graph namespace
(`policy_system_graphrag_spike`, separate from the production `policy_system`
graph in the same FalkorDB instance) and stands alone for comparison.

## Files

- `schema.py` — GraphRAG-SDK `GraphSchema` mirroring `ps-domain-concepts.md`'s
  10 node labels / 13 edge types (labels + descriptions + valid entity-pair
  patterns only — see "Convergence approach" below for why it stops there).
- `ingest.py` — ingests CRA/NIS2/GDPR PDFs (`docs/regulations/*.pdf`) and
  `engineering-practices-narrative.md` into `policy_system_graphrag_spike`,
  via Ollama by default or Azure Foundry with `--backend azure`.
- `engineering-practices-narrative.md` — prose rewrite of
  `engineering-practices-seed.json`'s full spine (6 Roles, 10 Requirements,
  9 Obligations, 10 PracticeAreas, 6 RiskPaths, 10 Capabilities, 10
  Policies, 10 Standards, 10 Controls), written so the SDK extracts from
  real narrative text rather than being handed a pre-built graph.
- `compare.py` — structural comparison: node/edge counts per type against
  `ps-domain-concepts.md`'s expected set, and cross-regulation `Capability`
  convergence, run against both graphs.
- `docs/azure-foundry-setup.md` — self-serve Azure Foundry provisioning
  guide. Not run — see "Backend sequencing" below.

## Setup

```bash
cd ../..   # repo root
pip install -r spikes/pipeline-rag/requirements.txt

# FalkorDB is already running in this environment (podman, localhost:6379,
# holding the production `policy_system` graph). Nothing to start.

# Ollama backend (default): gemma4:12b + nomic-embed-text are already pulled.
```

## Run

```bash
python spikes/pipeline-rag/ingest.py --source all --backend ollama
python spikes/pipeline-rag/compare.py
```

`ingest.py --source` also accepts `cra`, `nis2`, `gdpr`, or `engprac`
individually — useful for a first smoke-test run before committing to all
four.

### Model choice (revised after a first real run)

The first CRA smoke test ran against `qwen3-coder-next:q8_0` (84GB) and hit
real trouble, not just slowness: every extraction call took long enough to
hit an internal ~600s LiteLLM/Ollama timeout that `ingest.py` wasn't
overriding, and several chunks failed extraction outright after 3 retries
each. Root cause and fix, both now in `ingest.py`:

- **Timeout wasn't set explicitly.** `GraphRAG`'s own per-call timeout only
  applies when a `latency_budget_ms` is set on the ingest `ctx` (this script
  never sets one), so the call fell through to an internal LiteLLM/Ollama
  default rather than anything this script controlled. `ingest.py` now
  passes `--timeout` (default 1800s) into `LiteLLM(...)`/`LiteLLMEmbedder(...)`
  explicitly.
- **Default model swapped to `gemma4:12b`** (7.6GB) — general-purpose
  instruction model, small enough to actually finish extraction calls
  instead of racing a 30-minute timeout on every chunk. `qwen3-coder-next`
  remains available via `--model ollama_chat/qwen3-coder-next:q4_K_M` (or
  `:q8_0`) if a later run wants to check whether extraction *quality*
  actually improves with a bigger model, once the pipeline mechanics are
  proven out on something fast.
- **`--max-concurrency` default lowered to 1.** The SDK's own default is 3;
  against one local GPU that's contention, not parallelism, and it compounds
  the timeout risk above. Raise it deliberately for a remote/Azure backend.
- **`num_ctx` capped to 8192.** Even with the explicit 1800s timeout above,
  `gemma4:12b` still stalled every call. `ps -www -p <llama-server pid>`
  showed Ollama had loaded the model with `-c 262144` — its full
  architecture-max context — because neither GraphRAG-SDK nor LiteLLM's
  Ollama call was setting `num_ctx`, so Ollama fell back to the model's max
  instead of a sane default. None of this spike's chunks come close to
  needing that; `ingest.py` now passes `num_ctx=8192` explicitly.

## Backend sequencing (decided, see conversation this spike was scoped in)

1. **Ollama first** — local, zero cost, model already pulled. Get the
   pipeline working end-to-end here before touching Azure.
2. **Azure Foundry as an explicit follow-up** — `docs/azure-foundry-setup.md`
   documents provisioning against whichever subscription is `az`'s active
   default (confirm with `az account show`), but no `az` command from that
   doc runs without a separate, explicit go-ahead confirming resource group,
   region, model deployments, and cost.

## Comparison plan

Four dimensions, decided before scaffolding:

1. **Structural parity** — node/edge counts per type, schema conformance
   (any label/edge type outside `ps-domain-concepts.md`'s 10/13 flagged as
   unexpected). Mechanized in `compare.py`.
2. **Content fidelity** — spot-check extracted `Obligation`/`Capability`
   text against the source regulation articles on a hand-picked subset (not
   yet chosen — pick after a first real ingestion run, so the subset is
   drawn from what the SDK actually produced, not a pre-guessed list).
   Manual.
3. **Convergence quality** — GraphRAG-SDK auto-converges entities on exact
   `name + type` match only (deterministic `"name__type"` IDs, confirmed
   against the SDK's `graph-schema.md`); the current pipeline instead uses
   TF-IDF near-duplicate matching + human review
   (`tools/graph-ingestion/find_capability_duplicates.py` /
   `merge_capabilities.py`). Decision: use the SDK's native convergence
   as-is and report how many true cross-regulation `Capability` duplicates
   it catches vs. misses, as a metric — not a gap to patch with a custom
   post-processing pass. `compare.py`'s convergence check covers the
   mechanized half of this (how many `Capability` nodes end up shared
   across >1 `Regulation`); which *should have* converged but didn't is
   still a manual read against the source text.
4. **Falsification-step compatibility** — does `tools/skills/falsification-step.md`
   work unchanged against `policy_system_graphrag_spike`? It's invoked
   through `spikes/e2e-pipeline/ps.py cypher "<QUERY>" --graph
   policy_system_graphrag_spike` (that script takes `--graph`, confirmed
   before writing this). Manual: run the same falsification pilot shape as
   `spikes/pipeline3/smoke-test/run-01-falsification-pilot.md` against this
   graph once ingestion succeeds.

## Known gaps going in (documented, not yet resolved)

- **No custom ID/hash convention.** `ps-domain-concepts.md` derives
  `Capability` identity from `name` alone specifically to force
  cross-regulation convergence (`cap_{slug}_{hash}`); `Requirement`/
  `Standard`/`Control` use weak-entity IDs tied to their parent. GraphRAG-SDK
  has no hook for this — entity IDs are always `"name__type"`. Not worked
  around; tracked as comparison dimension 3 above.
- **PDF vs. markdown as extraction input.** `docs/regulations/` has both
  `CRA.pdf`/`CRA.md` etc. This spike ingests the raw PDFs (decided, to test
  the SDK's own PDF chunking/extraction rather than relying on the repo's
  existing markdown conversion as a crutch). If PDF extraction quality looks
  like the dominant source of error rather than the SDK's entity/relation
  extraction itself, re-running against the `.md` files is a cheap follow-up
  to isolate that.
- **No ingestion-time validation in GraphRAG-SDK.** Its docs assert
  "benchmark-leading accuracy" without detailing a validation mechanism
  beyond LLM-verified dedup. The current pipeline doesn't validate at
  ingestion time either (`falsification-step.md` runs at answer-time, not
  load-time) — so this isn't a regression, just worth confirming stays true
  once real data is in the graph.

## What this is NOT

- Not a replacement for `spikes/graph-ingestion3`/`tools/graph-ingestion`
  yet — this is the exploration that determines whether a replacement is
  warranted, per the four comparison dimensions above.
- Not touching the production `policy_system` graph — everything here
  writes to `policy_system_graphrag_spike` in the same FalkorDB instance.
- Not provisioning any cloud resource on its own — `docs/azure-foundry-setup.md`
  is documentation only until explicitly run.
