<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: GraphRAG-SDK native graph → policy_system graph (CRA only)

**Status:** Design, 2026-08-16. Consumes the output of `spikes/pipeline-rag3/`.

## Purpose

Test whether a `policy_system`-shaped graph can be produced **directly**
from GraphRAG-SDK's native intermediate graph (`policy_system_graphrag_native`,
produced by `pipeline-rag3`) — graph to graph, via a live FalkorDB connection —
without going through an intermediate JSON file.

This is the actual test of the hypothesis behind this line of spikes:
can a professional extraction package (GraphRAG-SDK) replace hand-crafted
LLM-prompt-to-JSON extraction — not just imitate its output format.

## Scope

- **Input:** `policy_system_graphrag_native` (from pipeline-rag3), CRA only.
- **Output:** a graph in domain shape (working name: `policy_system_graphrag_final`),
  written directly from the native graph — no JSON file in the production path.
- **Not in scope:** GDPR/NIS2, cross-regulation convergence
   (`find_capability_duplicates.py`/`merge_capabilities.py` — not meaningful
  with only one regulation loaded), changes to `load_graph.py` or the
  hand-crafted JSON pipeline (`test-data/eu-regulations/cra.json` remains
  the fidelity oracle for comparison — see Acceptance criteria).

## Acceptance criteria

**"Can we produce a `policy_system`-shaped graph from the intermediate
graph that is very close / similar to the existing `policy_system`
graph?"** Scoped to CRA only.

- **Baseline:** the `policy_system` graph as produced by
   `tools/graph-ingestion/load_all.sh` from `test-data/eu-regulations/cra.json`.
- **Comparison tool:** `compare.py` (in this spike). It already diffs two live
  FalkorDB graphs structurally (node/edge counts by label/type) with handling
  for GraphRAG-SDK's native `__Entity__`/`RELATES` shape — extend rather than
  rewrite.
- **"Very close / similar" is not yet precisely defined.** Structural parity
  (counts, label/type coverage) is mechanizable via `compare.py`. Content
  fidelity (does an Obligation's text actually match) stays a manual/
  qualitative check.

---

## Handoff from pipeline-rag3

### Input graph: `policy_system_graphrag_native`
Status: populated as of 2026-08-16 by pr3 `ingest.py --substantive 30 --spread`.
160 nodes, 318 edges, 0 errors, wall time ~480s.

**Node labels** (domain entities only, structural labels excluded):
| Label        | Count | Baseline | Notes |
|-------------|-------|----------|-------|
| Role         | 26     | 19        | 137% coverage — good          |
| Requirement | 36     | 287       | 13% — sparse, expected for 15-chunk sample |
| Obligation   | 24     | 349       | 7%                           |
| Capability   | 20     | 71        | 28%                          |
| Regulation   | 19     | 4         | ~94% cross-ref noise (external acts cited by CRA) |
| PracticeArea| 2      | 10        | 20%                          |
| Standard     | 1      | 10        | 10%                          |
| Policy       | 0      | 10        | ABSENT                        |
| Control      | 0      | 10        | ABSENT                        |
| RiskPath     | 0      | 6         | ABSENT                        |

**Edge types** (domain `RELATES`, structural excluded):
| Edge type       | Count | Baseline |
|---------------|-------|----------|
| DEFINES         | 29     | 19       |
| EXPRESSES       | 22     | 287      |
| HAS             | 20     | 349      |
| REQUIRES        | 9      | 396      |
| SUPERSEDED_BY   | 6      | 0        |
| SATISFIED_BY    | 0      | 354      |
| COVERS, OWNS,   | 0      | 10/10    |
| MITIGATED_BY,   |        |          |
| SUPPORTED_BY,   |        | 10/10    |
| IMPLEMENTED_BY, |        | 10/10    |
| VERIFIED_BY     | 10     |          |

**Structural labels/edges** (skip in `map_graph.py`):
`Chunk`, `Document`, `__GraphRAGConfig__` — 37 nodes; `MENTIONED_IN`, `PART_OF`,
`NEXT_CHUNK` — 226 edges.

### Issues `map_graph.py` must handle

**1. REQUIRES vs SATISFIED_BY (critical for convergence check)**
Native graph emits `REQUIRES: Obligation→Capability` (9 edges, 100% present).
Domain model uses `SATISFIED_BY: Requirement→Obligation` (354 in baseline).
These are different edges at different positions in the value chain.
`compare.py`'s `capability_convergence()` explicitly matches on `SATISFIED_BY`.
→ `map_graph.py` must: (a) keep `REQUIRES: Obl→Cap` as-is, AND
   (b) synthesize `SATISFIED_BY: Req→Obl` from the existing Req + Obl nodes
    or rename+reconnect. Without this, convergence check returns 0 always.

**2. Cross-reference Regulation filtering**
~94% of the 19 Regulation nodes are external EU acts cited by CRA
(NIS2 Directive, DORA, AI Act, GDPR, ENISA, national laws, procurement
directives, Annex references). Only 3-4 are the actual CRA.
→ Filter using `regulation_map.json` (see pr2 PROGRESS: "fully derivable
   locally from `graph-ingestion3/{cra,nis2,gdpr}.json`").
   Canonical CRA id = `CRA-1.0` (from `document_id="CRA-1.0"` in `ingest.py`).

**3. `capability_type` → domain `type` mapping (DEFECT-1 fix)**
Native graph: `Capability.capability_type = 'technical' | 'organizational'`
(separate property; SDK discriminator `n.type` is preserved as `'Capability'`).
Domain shape: `Capability.type = 'technical' | 'organizational'`.
→ `map_graph.py` must copy `n.capability_type` → `n.type` for Capability nodes.
   For other node types (Obligation.obligation_type etc.), pass through as needed.

**4. `status=null` → default to `'active'`**
All Obligation/Capability nodes have `status=None`. Domain model expects
`status` in `{active, draft, deprecated}`.
→ `map_graph.py` must default `status=NULL` to `'active'`
   (confirmed in pr2 PROGRESS: "status=None for all → transform.py must
    DEFAULT status to 'active'").

**5. Governance layer absent (Policy/Standard/Control/RiskPath = 0)**
The `--substantive /shall|should/` content filter captures obligation-dense
chunks. Governance/policy text (organizational commitments, standards,
controls) doesn't use "shall" — it uses "must be established", "should
be documented", or is in annexes.
→ Either:
   (a) Accept: 0/10 for Policy/Standard/Control/RiskPath is expected for
        a shall-filtered 15-chunk sample; flag in compare.py output.
   (b) Re-ingest with broader sample (no `--substantive` cap, or
        `--filter-regex 'shall|should|must|establish|document')`
        before `map_graph` runs.
   Decision belongs to user before rag4 execution.

**6. Pruned edges are gone**
35/72 domain relationships (48.6%) were pruned during pr3 ingestion
(all endpoint-type mismatches, not direction inversions).
The pruned edges are NOT recoverable post-ingestion.
`map_graph.py` works with what survived: DEFINES=29, EXPRESSES=22,
HAS=20, REQUIRES=9 = 80 surviving domain edges.

### Extraction quality (content spot-check from pr3)
All 15 spot-checked core-chain edges are semantically correct.
Convergence points are present and meaningful:
- "Inform Manufacturer of Vulnerability" → "Vulnerability Reporting" ✓
- "Take Corrective Measures or Withdraw/Recall Product"
  → "Market Suspension and Recall Management" ✓
- "Distributor" → "Inform Manufacturer of Vulnerability" ✓

### FalkorDB API notes
- Use `db.select_graph(name).query(cypher)`, NOT `db.execute()`.
  (`FalkorDB` object has no `.execute` method → AttributeError.)
- Cypher requires `AS` aliases: `count(n) AS c` — bare `count(n) c` errors.
- `compare.py` uses these correctly; ad-hoc probes must too.
- `db.list_graphs()` returns an iterable; there's no `.list_graphs()` on
   the `Graph` object itself.

### Sub-agent / model strategy
- `qwen3-coder-next:q4_K_M` (51GB): only ollama model that reliably
   executes tool calls via `pi` (writes files, runs commands).
- `qwen2.5-coder:14b` (9GB): BROKEN for pi — 32k context window causes
   truncation; outputs JSON tool-call syntax to stdout instead of executing.
- `glm-4.7-flash:q8_0` (31GB): fast for small mechanical tasks; default
   in `run_worker.sh`.
- `qwen3.8:27b-mlx` (18GB): design/RCA (262k context).
- Rule: use `qwen3-coder-next:q4_K_M` for any sub-agent that must write files.
   Never put two agents on the same model concurrently.

### Automated acceptance bar (proposed, for rag4)
| Check                                      | Threshold   | 30-chunk pr3 result |
|-------------------------------------------|------------|---------------------|
| Domain entities (excluding structural)     | ≥ 80       | 128 → PASS          |
| Core chain edge types present (≥4 of 4)    | DEFINES,EXPRESSES,HAS,REQUIRES all > 0 | PASS |
| DEFECT-1 regression (`n.type` collision)   | = 0        | 0 → PASS            |
| UNKNOWN-labeled entities                   | = 0        | 0 → PASS            |
| LLM NER JSON failures (per-run)            | < 10%      | 4/30 = 13% → MARGINAL |
| Cross-ref Regulation nodes                 | < 50% of Regs | 94% → FAIL (out-of-scope for rag3; must be FIXED in map_graph.py via regulation_map.json) |

### Azure config (if re-ingestion is needed)
```bash
export AZURE_API_KEY=$(az cognitiveservices account keys list \
    --name policy-system-graphrag-spike \
    --resource-group rg-policy-system-graphrag-spike \
    --query "key1" -o tsv | tr -d '\n')
export AZURE_API_BASE="https://policy-system-graphrag-spike.openai.azure.com/"
export AZURE_API_VERSION="2024-10-21"
```
`| tr -d '\n'` is required: `-o tsv` output carries a trailing newline;
auth silently fails (401) without it.

### File inventory

| File | Purpose | Status |
|---|---|---|
| `README.md` | This document | Updated with pr3 handoff |
| `map_graph.py` | Reads `policy_system_graphrag_native`, writes into `policy_system_graphrag_final` via FalkorDB driver — no JSON intermediate; must handle items 1-5 above | To write |
| `compare.py` | Structural comparison against `policy_system` baseline; convergence check uses `SATISFIED_BY` (must be created by `map_graph.py`) | Copied; default `--graphrag-graph` points to `policy_system_graphrag_spike` — MUST BE CHANGED to `policy_system_graphrag_final` |
| `regulation_map.json` | Canonical regulation id for cross-ref filtering; derive from `graph-ingestion3/{cra,nis2,gdpr}.json` | To write |
| `graphrag-sdk-configuration.md` | SDK reference (from pr3) — consult `spikes/pipeline-rag3/graphrag-sdk-configuration.md` | Referenced |


## Key facts for implementer

**Native graph schema (must be understood before writing map_graph.py):**
- Every domain entity: `:__Entity__` label + `n.type` property
   (value = the entity label as declared in `schema.py`).
   NOT guaranteed to be in `labels(n)[0]` —
    observed both `['Regulation', '__Entity__']` and `['__Entity__', 'Role']`.
- Every domain relationship: `:RELATES` edge type + `r.rel_type` property.
- Capability classification: `n.capability_type` property (NOT `n.type`).
- Obligation has: `n.confidence` (float), `n.obligation_type`
   ('technical'|'organizational').
- `n.status` is null for all nodes → default to `'active'`.
- Document-level structural nodes (`Chunk`, `Document`, `__GraphRAGConfig__`)
   and structural edges (`PART_OF`, `NEXT_CHUNK`, `MENTIONED_IN`)
   must be skipped entirely — they are not domain data.

**Baseline reference:** `policy_system` in FalkorDB at :6379
(776 nodes, 1475 edges from `tools/graph-ingestion/load_all.sh`).
Already loaded. Do not re-load unless graph was deleted.

**`compare.py` convergence check** (`capability_convergence()`)
looks for the full 4-edge chain
`EXPRESSES (Reg→Req) → SATISFIED_BY (Req→Obl) → REQUIRES (Obl→Cap)`
matching cross-Regulation Capability convergence.
With only CRA ingested, convergence by definition returns 0 or very few —
this is expected, not a bug. The check must pass without erroring first.
