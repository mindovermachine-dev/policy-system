# Brief: create an implementation plan for pipeline-rag3 ACs

## HARD RULES
- Output goes to `spikes/pipeline-rag3/docs/plans/plan.md`. Create the file.
- Read these files first (in `spikes/pipeline-rag3/`):
    `README.md`, `PROGRESS.md`, `LEARNINGS.md`, `graphrag-sdk-configuration.md`,
    `ingest.py`, `compare.py`, `schema.py`, `run_worker.sh`.
    Also read `spikes/CONVENTIONS.md` (in the repo root).
- Do NOT write any Python/shell code. Do NOT run any commands. Do NOT make
    network calls. This is a pure planning task.
- The plan must be concrete: each step has an explicit command or sub-agent
    brief, expected output, and exit criteria.

## Context — what exists, what's done

**Done (verified by leader, not by sub-agent summary):**
- `ingest.py` exists (295 lines), AST-parses, `--help` works. CRA-only,
    native-graph output, graph name `policy_system_graphrag_native`.
- `compare.py` exists, uses the correct graph names by default.
- `schema.py` (DEFECT-1 fixed: `capability_type` not `type`).
- `ratelimit.py` exists.
- `run_worker.sh` is proven (FINISHED / WATCHDOG_KILLED / EXIT_N).
- Baseline `policy_system` graph loaded in FalkorDB (776n / 1475e).
- `policy_system_graphrag_native` is empty (0n / 0e) — needs ingestion.
- CRA.pdf exists at `docs/regulations/CRA.pdf`.
- pr3 `.venv` = clone of pr2 (graphrag_sdk 1.4.0, works locally).
- Azure `policy-system-graphrag-spike` (swedencentral, kind=OpenAI, S0).
    Key fetchable via `az cognitiveservices account keys list`.
- `AZURE_API_BASE` and `AZURE_API_VERSION` NOT yet discovered.
- ollama available. `glm-4.7-flash:q8_0`, `qwen3-coder-next:q4_K_M`,
    `qwen2.5-coder:14b`, `qwen3.8:27b-mlx` all present.

**What's NOT done:**
- Azure API base + version unknown.
- No ingestion has been run yet — the graph is empty.
- `ingest.py` uses `entity_extractor=LLMExtractor(llm)` but this is
    untested; the `graphrag-sdk-configuration.md` notes that the default
    Step-1 extractor is GLiNER and that swapping to LLM is a high-signal
    untested lever. But that's a quality optimization — first get any run
    at all.

## Acceptance criteria (from README)
1. Run ingestion against CRA.pdf, land a **non-trivial graph** in
    `policy_system_graphrag_native`.
2. `compare.py` gives a structural read vs `policy_system` baseline
    (informational only).
3. Manual inspection decides proceed-to-rag4 + automated bar design.

## What the plan must answer

### Part A — Discovery
1. How to get `AZURE_API_KEY` (command, what to verify, where to store it
    for the sub-agent run).
2. How to discover/verify `AZURE_API_BASE` and `AZURE_API_VERSION`.
    The Azure account is `policy-system-graphrag-spike`, swedencentral,
    kind=OpenAI. What are the correct values? What's the right API version
    for graphrag-sdk's `LiteLLM` / litellm call path?
3. How to verify the credentials work before spending a full run.
    (1-chunk dry run is the plan — define the exact command.)

### Part B — 1-chunk dry run
4. Exact command to do a 1-chunk dry run. Use `--reset` (scoped to target
    graph only). Use `--max-chunks 1` OR `--substantive 3 --filter-regex
    /shall/i --spread` — pick one and justify.
5. What to look for in the output. What would make this a "proof of
    mechanism" vs a failure.
6. Semantic logging: what events should appear in the JSONL log.

### Part C — Scale-up decision
7. After a successful dry run, what's the right scale for "non-trivial
    graph"? CRA.pdf is ~1.7MB. Define the chunk count or `--substantive N`
    value that gives a structurally meaningful graph without burning
    excessive Azure calls. Consider the `--substantive` + `--filter-regex
    /shall/i --spread` path vs uncapped.
8. Watchdog timing: CRA full-text chunking + LLM extraction per chunk.
    Estimate wall time for the dry run and for a full/substantial run.
    What watchdog timeouts are appropriate?

### Part D — compare.py
9. After ingestion succeeds, how to run `compare.py` and what output to
    expect. Does `compare.py` need any fixes? Read it carefully.
10. What structural findings would indicate ingestion is "good enough"?
    (Don't set a fixed threshold — but say what you'd look for.)

### Part E — Iteration
11. What to do if ingestion fails at any step. Error taxonomy.
12. If the 1-chunk run succeeds but the full run fails partway, what's the
    recovery path? (The graph is reset-able.)

## Output format
Write a structured plan to `spikes/pipeline-rag3/docs/plans/plan.md` with
sections matching Parts A–E above. Each step should be numbered, have a
concrete action (command or brief), expected output, and a verification
criterion. Reference specific files and line numbers where relevant.

Also note: the user directive says to use `qwen3-coder-next:q4_K_M` and
`qwen2.5-coder:14b` as the available sub-agent models. Note which model is
best for which task type in the plan.
