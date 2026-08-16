# © 2026 Cartman ApS. All rights reserved.
# LEARNINGS — pipeline-rag3   (rolling; concise, essentials only)

## Environment
- E1 ollama local, 22 models; sub-agents = local (free). Use `run_worker.sh`
     (scoped pi worker, watchdog, semantic jsonl log + transcript). Default model
     `glm-4.7-flash:q8_0`; coder/file-write → `qwen3-coder-next:q4_K_M`.
     Avoid two sub-agents on the same big model at once (shared ollama; OOM risk).
- E2 Azure OpenAI `policy-system-graphrag-spike`, swedencentral. Key via
     `az cognitiveservices account keys list --name ... --query key1 -o tsv | tr -d '\n'`.
     `az ... openai deployment list` FAILS (openai ext missing). Don't rely on it.
- E3 FalkorDB on :6379 (VM/gvproxy proxy). Client = `falkordb.FalkorDB`
     using `.select_graph(name).query(cypher)`. NO `.execute()`.
- E4 pr2 venv cloned to pr3/.venv. graphrag_sdk 1.4.0.
- E5 `qwen2.5-coder:14b` CANNOT write files via pi (~32k ctx → truncation, JSON
     syntax not executed). Only `qwen3-coder-next:q4_K_M` or `qwen3-coder-next:q8_0`
     reliably perform tool calls. Per pr2 PROGRESS F2.

## Ingestion facts
- I1 Native = `:__Entity__` nodes (type on `n.type`) + `:RELATES` edges with
     `r.rel_type`. compare.py gates on those markers. Correct.
- I2 Native emits Obligation→Capability as **REQUIRES**, NOT SATISFIED_BY.
     transform.py (rag4) must map REQUIRES→SATISFIED_BY via Requirement.
- I3 DEFECT-1: Capability schema attr was `type` → clobbered SDK discriminator;
     FIXED → `capability_type` in pr3 schema.py. Verified: 0 collision nodes.
- I4 Over-extraction: 30 substantive chunks → 128 domain entities.
     Cross-ref Regulation nodes (~94% of Reg nodes) are from content-dense chunks
     that cite external acts. Fix belongs in rag4/transform.py via regulation_map.json.
- I5 gpt-5.4-mini is a reasoning model: NO `num_ctx`; embed dim MUST be 1536.
- I6 FilteringChunker: `--substantive N --filter-regex 'shall|should' --spread`
     selects N evenly-spaced chunks from the ~250 that contain 'shall'/'should'
     (CRA body: pages 31–70, 0 in first 10 pages).
     `--filter-regex` takes a BARE Python regex. Shorthand `/shall/i` is WRONG
     (matches literal string, 0 results). FIXED via `_normalise_regex()` in
     ingest.py: auto-strips `/.../flag` form, logs warning, caller ORs re.I.

## Risks / gotchas
- G1 First real Azure run spends some Azure. 1-chunk dry run first.
- G2 Regex: double-escape (\\b) caused 0-match in pr2. Shorthand /.../form caused
     0-match in pr3 (sub-agent passed /shall/i literally). Both fixed.
     Use plain `shall|should` or rely on the default.
- G3 Azure API key from `az ... -o tsv` has trailing newline. Pipe through
     `| tr -d '\n'` before `export`. Silent 401 otherwise.
- G4 `finalize()` raises non-critical "Attribute 'embedding' is already indexed"
     ERRORs (index re-creation after they're created in the pipeline step).
     These are benign. errors_count=0 in JSONL log despite the stderr ERRORs.
- G5 `_normalise_regex()` is in ingest.py as of this session (session 4). If
     ingest.py is regenerated from scratch, the fix may be lost.

## TODO
- T1 author pr3 source: ingest.py, compare.py, ratelimit.py → DONE
- T2 load baseline policy_system (load_all.sh, free) → DONE
- T3 1-chunk dry run; scale to --substantive 30 → DONE
- T4 (optional) full-corpus ingestion (266 chunks, no --substantive cap)

## Harness RCA (2026-08-16; the run_worker.sh "stall" was telemetry, not a hang)
- H1 The sub-agent did NOT hang: qwen3-coder-next produced a valid, import-clean
     ingest.py in ~2 min. The "stall" perception came from 3 telemetry gaps:
      (a) run_worker.sh printed the watchdog-cleanup's own `Terminated: 15` → misread
         as a kill;
      (b) bare `wait 2>/dev/null` blocked in bash 3.2 on the detached watchdog child
         → the SCRIPT itself hung;
      (c) the detached `sleep $WD` inherited the caller's stdout and held a piped
         pipe open → a downstream `| grep`/tool blocked until it self-terminated.
- H2 System bash is 3.2 (no `wait -n`, no GNU `timeout`). Watchdog design: `wait`
     ONLY the explicit pi pid, NEVER bare; watchdog subshell fds DETACHED
     (`>/dev/null 2>>$ERR`) so the orphan `sleep` can't hold a pipe open; outcome
     classification FINISHED / WATCHDOG_KILLED(143) / EXIT_N; artifact mtime+size
     before/after delta. PROVEN: 3 cases all return fast, piped or not.
- H3 Signal: `signal-cli send +4553860041 --message "..."` returns exit 0 + a ts.
     WARN "last received 35 days ago" refers to INBOUND only; sending works fine.
- H4 FalkorDB Cypher needs `… AS alias` (space-alias `count(n) c` errors).
     compare.py uses AS correctly; ad-hoc probes must use AS.

## Extraction quality spot-check (30-chunk run)
Core value chain, high quality:
   DEFINES: Product with digital elements → Manufacturer ✓
     HAS: Distributor → Inform Manufacturer of Vulnerability ✓
   REQUIRES: Inform Manufacturer of Vulnerability → Vulnerability Reporting ✓
(15 samples all semantically correct, convergence points present.)
