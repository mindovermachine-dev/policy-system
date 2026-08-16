# Implementation Plan — pipeline-rag3 (v2)

**Delta from plan.md:** Fixes to verified blockers and high-priority issues.

---

## A.1 — Fetch & Sanitize Azure API Key

| Old | New | Justification |
|-----|-----|---------------|
| `az cognitiveservices... > api_key.txt` | `az cognitiveservices... \| tr -d '\n' \| tee api_key.txt` | Key may have trailing newline; authentication fails with `\n` in key |

## A.2 — Derive & Verify Azure API Version

| Old | New | Justification |
|-----|-----|---------------|
| Assert `AZURE_API_VERSION=2024-10-21` is correct | Add **discovery step**:<br>1. Check if env var is already set via `echo $AZURE_API_VERSION`<br>2. If empty, run: `az cognitiveservices account list-deployments --resource-group ... --query "[?model.name=='gpt-5.4-mini'] | [0].model.version" -o tsv`<br>3. If step 2 fails (openai ext missing), use fallback `2024-10-21` per `graphrag-sdk-configuration.md` §2.7 | API version unverified; no external verification possible per `LEARNINGS.md` E2 |

## A.3 — Verify Credentials (1-chunk dry run)

| Old | New | Justification |
|-----|-----|---------------|
| Watchdog timeout: 120s (harness default) | Watchdog: **300s** (dry run, cold-start may take >180s) | First Azure call incurs ~30-60s cold-start; 120s watchdog kills process |

## B.1 — Run Dry Run Command

| Old | New | Justification |
|-----|-----|---------------|
| Watchdog timeout: 120s (harness default) | Watchdog: **300s** | Same cold-start concern as A.3 |

## C.2 — Full Run Command & Timeout

| Old | New | Justification |
|-----|-----|---------------|
| Watchdog: 600s | Watchdog: **1200s** in `run_worker.sh` | 15 chunks × (LLM extraction + embedding) × cold-start may exceed 10 min; 20 min provides buffer |

## Pre-flight — Baseline Graph Exists Check

| Old | New | Justification |
|-----|-----|---------------|
| No baseline verification | **Add step before any ingestion**:<br>`python -c "from falkordb import FalkorDB; db = FalkorDB(); g = db.select_graph('policy_system'); assert g.query('MATCH (n) RETURN COUNT(n) AS c').result_set[0][0] > 0"` | Baseline graph may be missing; compare.py fails if baseline doesn't exist |

## D.1 — Run compare.py

| Old | New | Justification |
|-----|-----|---------------|
| Command: `python compare.py` (assumed defaults) | Command: `python compare.py --graphrag-graph policy_system_graphrag_native --baseline-graph policy_system` | Explicit graph names prevent ambiguity; defaults *are* correct but should be stated explicitly |
