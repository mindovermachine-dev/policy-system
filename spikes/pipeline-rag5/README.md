<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: full-corpus, audited GraphRAG-SDK extraction → is it a *better* extractor?

**Status:** Design (2026-08-17). **Not run.** This document is the design; the
code and execution are the next step.

## Purpose

Answer, with evidence not vibes:

> **Can GraphRAG-SDK (native-PDF ingestion) produce a `policy_system`-shaped CRA
> conceptual-model graph *as good as* — or better than the  "policy_system_cra graph" already in falkordb**

The reference (`policy_system_cra`) is the thing we *have* — an LLM-on-`cra.md`
extraction that has been used to answer hard questions about the conceptual
model's elements. The candidate (`policy_system_graphrag_…_final_full`, this
spike) is a GraphRAG-SDK extraction of the *same* source. This spike makes the
comparison **fair** and turns "the SDK graph is smaller" (an ambiguous fact) into
a **reasoned verdict**.

## The confound this spike resolves (α / β / γ)

Any "SDK graph is smaller than `policy_system_cra`" result is *ambiguous* on its
own, because a small graph can mean three opposite things. This spike is built to
**disentangle** them:

- **α — engine-limit:** a fact is genuinely *not present even in the source that
   the SDK saw* — extraction failed. "SDK cannot match A on this" is defensible.
- **β — under-fed / filtered:** a fact *is* in the source but was dropped by a
   **sampling choice** (a chunk cap/filter) or by the SDK's **`_prune`** (a
   mandatory ontology-conformance step — see below) and is *recoverable* by
   changing that choice. "SDK cannot match A" is **not** defensible yet.
- **γ — regime gap:** a layer the SDK's open extraction *cannot produce by
   design* (it doesn't extract to a schema-grounded governance model), e.g. the
   Policy/Standard/Control/RiskPath layer. A *structural* gap, not an extractor
   defect.

**Verdict logic.** After a full-corpus ingest:
- B closes the gap on the parts A has ⇒ the small sample was the confound (**β**);
   the SDK is a viable extractor.
- B still falls short **and** the audit shows the lost signal was pruned/sampled
   but the *surviving* signal is sparse where A is dense ⇒ **α**: the extractor is
   the bottleneck.
- The deficit concentrates in layers the SDK cannot model (**γ**) ⇒ "the SDK is
   not a drop-in *for the schema-grounded governance model*," a nuanced but
   honest verdict — not a blanket "not better."

Two things the audit gives for free:
1. A **pruned-edge sidecar** (what the SDK silently threw away, *per reason*).
2. The **full raw LLM extraction** (`graph_data.extracted_relations` is retained
   through the pipeline), so we can check "was the signal *extracted at all*"
   independent of whether it *survived* pruning.

## `_prune` is mandatory and silent

GraphRAG-SDK's `IngestionPipeline` runs a fixed 9-step sequence. Two stages drop
data **without writing anything to the graph or returning it**:

- **Step 4b `_filter_quality`** — drops empty-id / dangling nodes+rels.
- **Step 5 `_prune`** — filters the extracted graph against the ontology:
   nodes whose `label` isn't declared (or `Unknown`) drop; relationships whose
   `rel_type` isn't declared, or whose `(src_label, tgt_label)` matches no
   `Relation.patterns`, drop. This is the bulk of the loss and is the hardest to
   see: a prior sample ingest lost ~half its relationships here.

`IngestionPipeline` is constructed **inside** `GraphRAG.ingest()` — `ingest()`
exposes `loader / chunker / extractor / resolver` but **no `pipeline=` slot**, and
`_prune` is private. So a clean subclass injection is **not** possible.

**Interception (F2):** patch `_prune` (+`_filter_quality`) **at runtime, in this
spike's own `ingest.py`** — not in `site-packages`. Each stage is
`graph_data → new GraphData`, so the dropped set is `input − output`
(`GraphNode` is keyed by `id`; `GraphRelationship` by
`start_node_id, end_node_id, type, rel_type`). Patch:

```
orig = IngestionPipeline._prune
def audit_prune(self, gd, ontology):
    out = orig(self, gd, ontology)
    # diff nodes & rels; classify each drop by reason; append to sidecar
    # raw extraction gd.extracted_relations is available here too
    return out
IngestionPipeline._prune = audit_prune
```

Guard the patch with an SDK-version check (`graphrag_sdk 1.4.0` pinned here): if
the signature of `_prune`/`GraphData` changes, the guard must **fail loudly**
rather than silently stop auditing. A partial capture is worse than none.

## Non-destructive, distinct graph names (never overwrite)

| Role | Graph / artifact | Notes |
|---|---|---|
| **Reference (A)** | `policy_system_cra` (FalkorDB, live) | LLM-on-`cra.md` extraction; the thing we're compared against. Consumed read-only. |
| **Candidate ingest (B native)** | `policy_system_graphrag_native_full` | full-cra.pdf, **new name** — never reuse `…_native` (the under-fed sample). |
| **Candidate mapped (B final)** | `policy_system_graphrag_final_full` | clean domain shape, via `map_graph.py`. **New name.** |
| **Pruned-edge audit** | `logs/pruned-<ts>.jsonl` | Sidecar; native graph stays clean for mapping. |
| **Run log** | `logs/ingest-<ts>.jsonl` | counts / wall / outcome (as in any ingest). |

`--reset` deletes only the *target* graph `…_native_full`; nothing else is
deleted.

## Scope

**In:** full `CRA.pdf` ingestion (no chunk cap, no `--substantive` filter);
prune/quality-filter audit; map to domain shape; compare vs `policy_system_cra`;
α/β/γ attribution; content spot-check. CRA only.

**Out:** GDPR/NIS2; cross-regulation convergence *as a graded target* (CRA-only ⇒
convergence is ~0 by definition — the check must *run* without error, that's the
bar, not ">0"); changes to `policy_system_cra` or any existing graph.
Re-running the under-fed sample is unnecessary here — this spike **re-ingests
fresh**, no dependency on the prior native graph.

## The three stages

1. **Ingest + audit** — `ingest.py` (seeded from `pipeline-rag3/ingest.py`),
   full corpus by default, with the runtime `_prune`/`_filter_quality` audit patch
   writing `logs/pruned-<ts>.jsonl`. Writes `policy_system_graphrag_native_full`
   via `GraphRAG(ontology=SCHEMA, …)`. `schema.py` (the ontology) is copied from
   `pipeline-rag3/schema.py` **verbatim** — it is the shared concept model.
2. **Map** — `map_graph.py` (copied from `pipeline-rag4/map_graph.py`), pointed
   at `…_native_full` → `…_final_full` (it is already parameterized:
   `native_name`, `final_name`, `--keep-set-path`). Preserves `n.name`, maps
   `capability_type → type`, `status null → active`, synthesizes `SATISFIED_BY`
   from shared-chunk co-occurrence, drops structural `Chunk`/`Document`/
   `MENTIONED_IN` nodes, cross-ref-regulation filter (keep-set
   `regulation_map.json`).
3. **Compare** — `compare.py` (copied from `pipeline-rag4/compare.py`), **adapted
   for CRA-vs-CRA**: diff `…_final_full` vs `policy_system_cra` — per-label and
   per-edge-type counts + *ratios* (A-vs-B), defect-1 (Capability `type`
   collision) = 0, unknown labels = 0, `capability_convergence()` runs without
   error. **Production gates removed for a CRA-vs-CRA comparison:** the
   cross-ref-% gate (a *production-path* check "is the extraction noise-free?" —
   meaningless when *both* graphs are CRA-only, and it would report the reference
   as "100% cross-ref → FAIL" purely because the CRA's id differs from the
   keep-set key).

## Sidecar schema (per dropped entity; append-only JSONL)

```jsonc
{ "ts": "...", "run_id": "...", "document_id": "CRA-1.0",
  "stage": "filter_quality" | "prune",
  "kind": "node" | "relationship",
  // node
  "id": "...", "label": "...", "reason": "empty_id" | "dangling" | "label_undeclared",
  // relationship
  "start": "...", "start_label": "...", "end": "...", "end_label": "...",
  "rel_type": "...", "source_ref": "Art. X", "keywords": "...", "weight": 1.0,
  "reason": "dangling" | "rel_type_undeclared" | "pattern_mismatch",
  "declared": [["src","tgt"]...] },   // only for pattern_mismatch
  "chunk_ids": [...]
```

## Runtime dependencies (named, not lineaged)

- **Source:** `docs/regulations/CRA.pdf` (repo root, up two levels).
- **Reference graph:** `policy_system_cra` in FalkorDB `localhost:6379` —
   live input, read-only.
- **Ontology:** `schema.py` (copied in; the shared concept model).
- **Mapping assets:** `map_graph.py`, `compare.py`, `regulation_map.json`
   (copied from `pipeline-rag4`; CRA keep-set `{cyber_resilience_act__regulation}`).
- **Harness (optional):** `run_worker.sh` for short scoped sub-agent tasks.
- **Backend:** Azure `gpt-5.4-mini` + `text-embedding-3-large` via litellm
   (`AZURE_API_KEY/BASE/VERSION` required; `| tr -d '\n'` — trailing newline
   breaks auth). Embedding dim 1536, matching this graph's vectors.
- **DB client:** `FalkorDB` via `db.select_graph(name).query(cypher)`; no
   `.execute`/`.node_count`; Cypher needs `… AS alias`.

## Files to create (build — **not** done here)

| File | From / what |
|---|---|
| `ingest.py` | `pipeline-rag3/ingest.py` + audit patch (F2) + full-corpus default (`--substantive`/`--max-chunks` off by default) + `--prune-log <path>` |
| `schema.py` | `pipeline-rag3/schema.py`, **verbatim** |
| `regulation_map.json` | `pipeline-rag4/regulation_map.json`, as-is |
| `map_graph.py` | `pipeline-rag4/map_graph.py`, as-is (already parameterized) |
| `compare.py` | `pipeline-rag4/compare.py` + CRA-vs-CRA adaptation (ratios; drop production cross-ref gate on the CRA baseline; keep convergence-runs + defect-1 + unknowns) |
| `PROGRESS.md` / `LEARNINGS.md` | spike convention |

Build order: copy → patch `ingest.py` → `--reset` `…_native_full` → full ingest
(audit to `logs/pruned-*.jsonl`) → `map_graph.py` → `compare.py`. **Stop after
design.**

## Acceptance criteria

- **AC-1 (ingest + audit).** `policy_system_graphrag_native_full` is populated,
   0 ingest errors; `logs/pruned-<ts>.jsonl` exists with the full dropped set
   classified by reason; native graph itself is unpolluted.
- **AC-2 (mapping).** `…_final_full` is clean domain shape: defect-1
   (`Capability.type` collision) = 0, unknown labels = 0, no `__Entity__`/
   `RELATES` leakage, `SATISFIED_BY` synthesized.
- **AC-3 (fair structural parity vs reference).** `compare.py` (adapted) runs
   clean on `…_final_full` vs `policy_system_cra`; per-label + per-edge-type
   counts **and ratios** reported; `capability_convergence()` runs without error
   (value ~0 expected, CRA-only). No production cross-ref gate on the reference.
- **AC-4 (α / β / γ attribution — the deliverable).** Using the pruned audit +
   the raw-extraction diff, classify B's gap vs A: which missing A-has elements
   were **pruned/sampled** (β, recoverable) vs **never extracted even when
   present in source chunks** (α, extractor limit) vs **in a layer the SDK can't
   model** (γ). Emit a one-paragraph verdict: *"SDK is / is not a better
   extractor, and why."*
- **AC-5 (content fidelity).** Manual spot-check of the core value chain
   (`Def → Requires → Satisfied-By → …`) against `cra.md` text for a few
   obligations/capabilities — qualitative, documented.

## Verdict interpretation

- B matches A on AC-3 where A has signal ⇒ **β** dominated: the sample was the
  confound; the SDK is a viable extractor.
- B still short **and** the lost signal was pruned/sampled while *surviving*
  signal is sparse where A is dense ⇒ **α**: extractor is the bottleneck; "not
  better" is defensible.
- Deficit concentrates in a layer the SDK can't model ⇒ **γ**: "not a drop-in
  for the governance layer; viable for the obligation/capability core" — the
  honest middle.

## Risks / open checks

- **Reference coverage.** Verify `policy_system_cra` itself spans *all* of
   `cra.md` (is A a full extraction or also a sample?), or the comparison is
   confounded the *other* way. Confirm A's article coverage before grading.
- **`_prune` patch fragility.** Runtime monkey-patch across SDK versions
   (1.4.0 pinned) — version-guard, fail loud. Worst case the audit is partial;
   flag, don't trust.
- **Full ingest cost.** ~9× the prior sample run (≈480s) → ~1h + ~9×
   `gpt-5.4-mini`/embedding tokens + `finalize()` cross-doc dedup. Confirm
   budget/appetite before the run.
- **Cross-ref filter on B still zeroes `EXPRESSES`** if all native `EXPRESSES`
   are cross-ref-sourced — a *consequence of correct filtering*, not a defect;
   report it, don't grade it.
- **No shared-infra changes.** Azure is the sole external dependency; no Ollama
  embedding-server restart; no `site-packages` edit (patch is runtime-only).
