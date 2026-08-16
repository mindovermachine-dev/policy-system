# pipeline-rag2 — progress tracker

Goal: prove whether `ingest.py` (GraphRAG-SDK) + `transform.py` (deterministic
20%) + existing `load_graph.py`/`merge_capabilities.py` can generate a graph that
is **as similar as possible** to the hand-curated `policy_system`. Adoption call
lands in `compare.py` (structural + convergence parity, then NP/NHQ answer parity).
Oracle "prove it's true" mechanism: deferred until a runnable second graph exists.

Stop: acceptance met OR ~8h soft bound. CRA-first, then NIS2/GDPR/ENGPRAC.
Discipline: no firefighting (RCA + log first, see logs/), semantic logging
everywhere, sub-agents run sequentially, leader reviews every worker artifact.

## Parameters
- Worker model: MECHANICAL CODE -> `glm-4.7-flash:q8_0` (128k, non-reasoning, ~5-15s, reliable
   tool-calls, F2); DESIGN/hard-RCA/reading -> `qwen3.8:27b-mlx` (262k, proven). Local ollama,
   `pi -p`, `--thinking off`. See F2 / Model plan.
- Ingestion LLM backend: **Azure `gpt-5.4-mini` + `text-embedding-3-large`** (B1, CLOSED).
- Ingestion concurrency: 2 (documented-safe vs 10-req/60s Azure limit; enforced by RateLimitedLLM).
- `--reset` scope: ONLY spike graphs `policy_system_graphrag_{native,final}`; NEVER baseline `policy_system`.

## Confirmed mechanisms
- [x] Sub-agent primitive `run_worker.sh NAME "TASK" [TOOLS] [WD]` -> fresh-context `pi -p`. Smoke OK.
- [x] `ollama` up; `qwen3.8:27b-mlx` loaded (GPU). `pi` provider `ollama` ready.
- [x] `graphrag-sdk 1.4.0` + `falkordb 1.7.1` + `litellm 1.97.0` in `.venv` (py3.14); cp314 wheels fine.
- [x] `GraphRAG.ingest()` accepts `loader=`/`chunker=`/`extractor=`/`resolver=` (3 independent per-call
   slots). No explicit IngestionPipeline needed. (sub-agent #1)
- [x] `MarkdownLoader`/`StructuralChunking`/`ContextualChunking` import ONLY from submodules
   (`graphrag_sdk.ingestion.loaders.*`, `...chunking_strategies.*`), not top-level.
- [x] `.md` input: `MarkdownLoader` DOES populate `document.elements` w/ breadcrumbs; the
   "flat-list -> fallback" fear is a NON-issue for markdown (HIGH conf, sub-agent #1).
- [x] `ratelimit.py` RateLimitedLLM: full 7-method LLMInterface ABC, shared Semaphore(2) + 10/60s
   sliding-window, pure-asyncio (no new dep). 3/3 self-tests PASS (RCA x1, x2). VERIFIED.
- [x] Azure wiring validated end-to-end: `gpt-5.4-mini` -> PONG; `text-embedding-3-large` dim=1536
   -> 1536-vec. NO `num_ctx` for Azure (Ollama-only; `gpt-5*` = reasoning path).

## Env / harness facts (probe 2026-08-15/16)
- System `python3` = 3.9.6 (no SDK); homebrew `python@3.14.7` + pip. venv `.venv` used.
- No `timeout` on macOS; watchdog in run_worker.sh uses bash `sleep`+`kill` (SIGTERM => status 143;
   on-kill the in-progress .py file is still on disk, but the final-text .out can be empty).

## Deliverables / backlog (from HANDOFF)
- [x] B1 resolve ingestion backend -> Azure (CLOSED)
- [x] Step-1 `GraphRAG.ingest()` API surface (loader=/chunker= accepted)
- [x] requirements.txt + isolated venv `.venv`
- [x] ratelimit.py (DONE + VERIFIED, sub-agent #2 + leader RCAs)
- [ ] ingest.py (UNBLOCKED: blueprint=docs/ingest-config-findings.md, ratelimit.py ready; sub-agent brief docs/briefs/ingest.md)
- [ ] regulation_map.json
- [ ] transform.py (ID assign, hub-skipping catch, cross-ref filter, property pass-through, direction correction)
- [ ] load via load_graph.py into policy_system_graphrag_final
- [ ] find/merge_capabilities.py convergence
- [ ] compare.py (structural + convergence + NP/NHQ answer parity)
- [ ] spike go/no-go report

## Sub-agent log
- #1 ingest-config (loader/chunker + Azure wiring + rate-limiter):
   docs/briefs/ingest-config.md -> docs/ingest-config-findings.md  [DONE, validated]
- #2 ratelimit.py + ingest.py (docs/briefs/ingest.md): SPLIT.
   ratelimit.py DONE+VERIFIED; ingest.py NOT written (worker SIGTERM at 30-min watchdog).
   - RCA1: `_FakeInner` test double under-implemented the 7-method `LLMInterface` ABC
     (missing sync `invoke`); tests could not instantiate -> fixed by adding `invoke()`.
   - RCA2 (REAL concurrency bug): `_reserve_rate_slot` referenced `wait_for` which was set
     only in the window-full branch (UnboundLocalError) + muddled while/break. Leader rewrote
     it as a clean sliding-window (under-lock trim; win => append+return, else compute wait;
     sleep outside the lock; loop). 3/3 self-tests PASS in 0m3.6s.
   - ingest.py not started; .out empty on kill (file persisted on disk, summary lost).
- #3 (next) ingest.py only: ratelimit.py is a FIXED dep (do not modify), tight brief,
   progress-flush discipline, ~900s watchdog, mock (fake-LLM) verification, NO real Azure.

## Decisions / findings
- D1 (THROUGHPUT) **RESOLVED 2026-08-16**: user chose OPTION A (hybrid). Mechanism fixed via F2.
   file and still shipped 2 real bugs. At this rate the 8h budget risks NOT meeting ACs.
   Open question to user: hybrid (big model for design/RCA, small/fast e.g. `phi4-mini:3.8b`/
   `qwen3:14b` for mechanical code like ingest.py) vs keep 27b for everything vs leader writes
   mechanical code directly (blueprint already in leader context). See chat.
- F1: `--max-concurrency` at `ingest()` level is a NO-OP for a single source path; per-chunk
   concurrency comes from `GraphExtraction(max_concurrency=2)` + the shared RateLimitedLLM.
- F2 (CONFIG RCA, 2026-08-16): sub-agent file-write failures = context-window, NOT model name.
   Every FAILED `pi` run used a 32k-ctx model (qwen3:14b[reasoning], qwen2.5-coder:14b); the only
   model that reliably wrote files (qwen3.8:27b-mlx) has 262144 ctx. pi's agent loop (system prompt
   + 27 skills + tool schemas + brief) exceeds 32k -> truncation -> garbage/no tool call. FIX:
    glm-4.7-flash:q8_0 (128k, non-reasoning, 3/3 correct in 4-15s) for mechanical code; 27b for
   design. Launch registry = `~/.pi/agent/models.json` (ctx + reasoning flags per model). run_worker.sh
   now DEFAULTS to glm-4.7-flash:q8_0. NEVER use a ~32k model.
- Model plan: MECHANICAL=glm-4.7-flash:q8_0 (DEFAULT, ~5-15s); DESIGN=harden-RCA= qwen3.8:27b-mlx
   (reserve). Both `--thinking off`. Both verified by the leader (27b twice shipped real bugs).

## Resolved
- B1 CLOSED 2026-08-15: backend = Azure `gpt-5.4-mini` + `text-embedding-3-large`. `az` authenticated
   (sub cosmos4biz-nonprd, Cartman ApS); resource `policy-system-graphrag-spike` live; key fetchable.
   Key used in-session only, NEVER written to disk.
- B2 CLOSED: py3.14 venv works (cp314 wheels exist).
- F1/MarkdownLoader-RISK CLOSED 2026-08-16: `MarkdownLoader` pops `document.elements` for .md;
   `StructuralChunking` = the lever; `ContextualChunking` kept as a rate-guarded alternative.

## Log index
- Run logs + transcripts: `logs/` (jsonl metadata + `.out`/`.err` per worker run).
- Briefs: `docs/briefs/`. Findings: `docs/ingest-config-findings.md`.
- RCA writeups: `logs/RCA-NNN-*.md`.

## D-A — CHUNKING PIVOT (2026-08-16, user decision, supersedes README/HANDOFF)
- Dropped MarkdownLoader+StructuralChunking header-breadcrumb lever: it is INERT on the actual
   inputs. Facts: .md is markitdown-derived (LOSSY) from .pdf; CRA.md/gdpr.md have 0 header +
    ~0 line-start Article N (NIS2.md has 188: source-inconsistent); and the SDK PdfLoader does
    not populate document.elements either. So the lever doesn't fire on this data, EITHER way.
- DECISION: PDF + flat robust chunker. EU regs -> authoritative .pdf via PdfLoader (pypdf
    backend, graceful; PyMuPDF [pdf-fast] AGPL-3.0 = optional LATER recall experiment).
    engprat -> .md narrative via MarkdownLoader (no PDF exists).
    Chunker = SentenceTokenCapChunking(max_tokens=512, overlap_sentences=2).
- Empirical before/after chunk test (option D of md/pdf discussion) ONLY IF flat chunking
    reintroduces the 71% pruning / hub-skipping concern; not a blocker now.

## CURRENT STATUS / NEXT ACTION (updated 2026-08-16 after D-A)
- ratelimit.py DONE + 3/3 self-tests PASS (2 RCAs by leader: ABC stub + wait_for unbound).
- ingest.py DONE + compiles + import-resolves + --help OK. Option A wiring verified: pdf for
    CRA/NIS2/GDPR, md for engprac, SentenceTokenCapChunking, CappedChunker, RateLimitedLLM +
    LLMExtractor + GraphExtraction(max_concurrency=2), scoped --reset, append-log.
- FalkorDB UP on 6379 (podman fresh --rm). Baseline policy_system likely needs a later
    load_all.sh before compare.py.
- NEXT = first REAL Azure 1-chunk dry-run `python ingest.py --source cra --max-chunks 1
     --graph-name dryrun_native` -- CHECKPOINT (external/spend). ~3 Azure calls (1 embed +
     1-2 extract), under 10/60s. Validates load(pdf)->chunk->LLM extract->embed->write->finalize.

## DRYRUN R1 — CRA 1-chunk, 2026-08-16 13:30Z  [PASS machinery, quality unknown]
- Full pipeline OK on Azure gpt-5.4-mini + text-embedding-3-large, pypdf, 5.95s, 0 errors.
   pypdf loaded CRA.pdf 81pg/369903ch -> 266 chunks -> CappedChunker=1 -> LLM extract/embed/write/finalize.
   Append-JSONL log (logs/ingest-*.jsonl) clean; RateLimitedLLM gate held.
- Benign: finalize re-runs ensure_indices -> "already indexed" ERRORs (non-fatal, errors=0).
- FINDING F3 (central): on chunk #1 (CRA preamble), LLM's single domain relationship was an
   off-target `EXPRESSES` (Regulation,Regulation) -> PRUNED by ontology. Pruning is DESTRUCTIVE
   (dropped before write) => the planned transform.py direction-correction table can only fix
   edges that SURVIVE; the real lever is schema/description quality (README: 61->38 pruned).
- Chunk #1 is unrepresentative (preamble; only the Regulation itself is extractable).
- NEED: a quality MEASUREMENT on substantive ('shall') content, not just the preamble.
- NEXT (pending user scope check-in): measure domain-edge landing+pruning on representative
   substantive chunks; inspect landed domain edges; decide schema-tuning vs sample size.

## Update — resume after context loss (this session)

Cadence: short PROGRESS updates at each checkpoint (no wall-clock timer exists;
flag if a gap > 30 min). Format below.

### DEFECT-1 (CAPABILITY `type` collision) — FIXED 2026-08-16
- Measured run (measure_native, CRA, --max-chunks 12, 227s, 0 err): 33 nodes /
   15 edges / 15 pruned / 43 mentions. Pruning was all endpoint-TYPE mismatch
    (EXPRESSES x4 + a DEFINES = Reg->Reg; self-loops COVERS x7, OWNS x3); NOT
    direction-of-valid-pair. 0 Obligation / 0 core-chain nodes landed.
- ROOT: schema.py Capability `Attribute name="type"` clobbered the SDK node
    discriminator `n.type` (value 'technical' overwrote the label). 8 "capability"
    nodes carried type='technical' and were un-discriminable.
- FIX: renamed native attribute `type` -> `capability_type` (transform.py will map
    native capability_type -> domain `type`). Verified: schema imports, ZERO
     attributes collide with {type,name,id,description}. Obligation
    confidence/obligation_type do NOT collide -> unchanged.

### FilteringChunker + `--substantive` / `--filter-regex` — ADDED, VERIFIED
- Free probe (pypdf): 'shall' concentrated pages 31-70; 0 in first 10 pages,
   0.4% of all 'shall' in first 30. => first-N sample is mathematically preamble.
- Added FilteringChunker (content filter, position-agnostic) + flags. Self-test
   PASS: filtered+cap+order+no-cap after fixing a real `.search` call bug
    (compiled re.Pattern is not callable; was `predicate(text)` -> `.search(text)`).
- Syntax + --help OK. NOT yet run against Azure.

### NEXT (pending user go = spends Azure)
`python ingest.py --source cra --substantive 12 --graph-name measure2_native --reset`
   -> substantive 'shall' chunks; expect core-chain + capability_type to populate.
   Then read native graph: landing/prune by pair, core-chain presence, attr pop.

### MEASURE3_NATIVE INSPECTION (this session — analysis complete, no spend)
Context recovered: PROGRESS was stale at "pending user go". The real last run in `logs/`
   is measure3_native (cra, --substantive 15, /shall/i), completed 16:09Z, 0 errors. A prior
   attempt at 16:02Z used /\\bshall\\b/ and matched 0 rows (regex double-escape -> flag typo,
   not a code defect; the good run used /shall/i).

WHAT LANDED (pulled from FalkorDB, read-only): 89 nodes / 158 edges.
- Node labels: Document 1, Chunk 15, __GraphRAGConfig__ 1, __Entity__ 72. Of the 72 domain-
   typed: Role 25, Requirement 21, Regulation 15, Obligation 5, Capability 6.
- Every domain-typed entity carries a `type` property -> DEFECT-1 fix HOLDS: no Capability
   'type' collision; capability_type is a separate attr (6/6 populated).
- Extended attrs populated: Obligation.confidence (0.91-0.99), Obligation.obligation_type
   (technical/organizational), Capability.capability_type (technical/organizational). status=None
   for all -> transform.py must DEFAULT status to 'active' (per HANDOFF).
- Domain edges (35, carry rel_type) — the CORE VALUE CHAIN IS COHERENT:
     Regulation --DEFINES--> Role             (13)
     Regulation --EXPRESSES--> Requirement   (10)
     Role         --HAS--> Obligation        (5)
     Obligation   --REQUIRES--> Capability    (5)   <-- convergence enabler PRESENT
     Regulation --SUPERSEDED_BY--> Regulation (2)
   NOTE: NO SATISFIED_BY edges — native emits Obligation->Capability as REQUIRES. transform.py's
   rel_type mapping must normalise REQUIRES->SATISFIED_BY. Partial answer to open Q#3: the lever
   is rel_type remap, not endpoint-direction flip.

QUALITY CONCERNS (feed the schema-tune-vs-scale decision):
- 72 domain entities from a 15-chunk sample = heavy over-extraction. The 15 'Regulation' nodes
   are mostly cross-ref/Article/Annex noise (Article 52(15), Annex VIII, National law, ENISA,
   Member State); only ~2 are the real CRA. -> transform.py cross-ref filtering + regulation_map.json
   (canonical id = CRA-1.0) is load-bearing.
- 24/25 Roles orphaned (only 'Manufacturer' links the chain); 11/21 Requirements orphaned; 1/6
   Capabilities orphaned. 15 chunks cannot exercise convergence (baseline = 349 Obligation /
   77 Capability / 50-65% dual-required).
- regulation_map.json is FULLY DERIVABLE LOCALLY from graph-ingestion3/{cra,nis2,gdpr}.json
   (Regulation id=CRA-1.0; role_/cap_<slug>_<sha1[:6]> scheme; source_ref ~ CRA-1.0_req_art_).
   engprac narrative already copied into this folder. Baseline `policy_system` is NOT in FalkorDB
   yet -> compare.py needs a local load_all.sh of the baseline first (free).

VERDICT: machinery is sound end-to-end on Azure (load-pdf -> chunk -> LLM-extract -> embed ->
   write -> finalize, 0 errors). The next meaningful step (full 4-reg + engprac ingestion into
   _final, then compare.py adoption call) is BLOCKED on: (a) Azure creds not in this env —
   ingest.py._require_azure_env() hard-blocks; key IS fetchable via `az` (authenticated, non-spend);
   (b) a multi-reg run is real Azure spend, which project discipline gates on user go.
   Open local deliverables: regulation_map.json, transform.py, compare.py scaffold + baseline load.
