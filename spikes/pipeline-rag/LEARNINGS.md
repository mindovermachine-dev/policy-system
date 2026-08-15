# © 2026 Cartman ApS. All rights reserved.
# Session learnings — 2026-08-14

Working notes from an extended session exploring whether GraphRAG-SDK can
replace `tools/graph-ingestion`. Written so a fresh session can pick up
without re-deriving everything. See `README.md` for the spike's original
scope/plan; this file is the running log of what was actually found.

## TL;DR

- **2026-08-14, later same day: `docs/graphrag-sdk-configuration.md` added** —
  a config-surface audit built from the *installed* `graphrag_sdk` 1.4.0
  source (the FalkorDB doc site's `strategies.md` was caught asserting
  `SemanticResolution`/`LLMVerifiedResolution` don't exist, contradicting
  this file directly — installed source used as ground truth instead). Found
  two things never isolated as variables in the experiments below: (1)
  every run to date left `entity_extractor` unset on `GraphExtraction`,
  defaulting Step 1 to `GLiNERExtractor` (a non-LLM, zero-shot NER model)
  rather than the chat LLM; (2) `LiteLLM` instances are hardcoded to
  `max_concurrency=12` regardless of `ingest.py`'s `--max-concurrency` flag,
  which only reaches the extraction step, not resolvers — this fully
  explains the "`LLMVerifiedResolution` confounded by uncontrolled
  concurrency" finding below. See "LLMExtractor Step-1 swap" further down
  for the first test of finding (1) — result was confounded by (2)'s cousin
  problem (LLMExtractor doubles LLM call volume, which made Azure
  rate-limiting worse mid-test) and is **not yet a clean signal**.
- **Local Ollama backend: parked, not resolved.** Both models tried hit
  real, distinct failure modes — not "the model is too weak." See
  "Ollama backend findings" below before touching this path again.
- **Azure backend: working, provisioned, live.** `gpt-5.4-mini` +
  `text-embedding-3-large` in `docs/azure-foundry-setup.md`'s target
  subscription. Full CRA ingest succeeded end-to-end.
- **Two real bugs found and fixed** in this spike's own code
  (`ingest.py`, `compare.py`) — see "Bugs found and fixed."
- **The dominant finding**: ~71% of extracted relationships get pruned at
  ingestion time for `(source, target)` type mismatches against the
  ontology — the model doesn't reliably follow `schema.py`'s declared
  relationship directions/patterns. This is the main open problem.
- **One fix attempt worked, partially**: rewriting relation descriptions
  with explicit direction + a worked example cut pruning by ~38% (61→38
  on a fixed test excerpt), with 3 of 13 relation types going to zero
  pruning. A follow-up attempt (anti-shortcut instructions on top of
  that) made it *worse* (38→49) — **⚠️ see "Current schema.py state" —
  the worse variant is what's currently live in the file.**
- **Other candidate fixes were tried and did NOT clearly work**:
  `SemanticResolution` (embedding-based dedup) did not merge a known
  duplicate pair even at a lowered threshold. `LLMVerifiedResolution` gave
  an ambiguous result, confounded by its own uncontrolled concurrency
  hitting the same rate limit that's already a known issue.
- **All of the above is n=1 per configuration.** Real run-to-run
  extraction variance was observed independent of any code change (raw
  node counts wobbled 64–76 across otherwise-identical runs). Don't treat
  any single comparison in this doc as statistically solid — see
  "Untested variables" for what real confidence would require.

---

## Ollama backend findings (parked)

Two models tried, two different failure modes — neither is "just use a
bigger model":

- **`gemma4:12b`**: a "thinking" model. With `num_ctx=8192` (needed to
  avoid an earlier hang — Ollama was loading at its full 262144-token
  architecture max), it burns the *entire* context budget on internal
  `reasoning_content` chain-of-thought before ever emitting the actual
  JSON answer. Confirmed directly: `finish_reason=length`,
  `completion_tokens=6693` all in `reasoning_content`, `content=""`.
  Every Step 2 (relation extraction) call failed this way — 0
  relationships extracted across a full 9-chunk run.
- **`phi3:mini`**: fast (7.7s) and correct when called synchronously
  (`litellm.completion`), but the SDK's real pipeline always calls async
  (`litellm.acompletion`, via `abatch_invoke`/`ainvoke`). The *first*
  async call after a fresh model load returned garbled, incoherent text
  in 1.4s (too fast to have actually processed the prompt) — looks like a
  cold-start bug in the local `llama-server`/Ollama stack, possibly
  related to `--context-shift`. A second async call, same prompt,
  succeeded cleanly. This doesn't fully explain why a full real run (model
  kept warm across 9 chunks) still got 0/9 relationships, though — the
  cold-start theory alone doesn't cover that. **Not fully root-caused.**
  Next step if resumed: rerun with per-chunk raw-response logging to see
  exactly which of N calls succeed vs. garble.

**Also found**: `ingest.py`'s `--max-concurrency` flag was a silent no-op
for single-source ingestion (`GraphRAG.ingest(source=<single path>)` only
threads `max_concurrency` through when `source` is a `list` — confirmed
against the SDK's own docstring and `_ingest_batch` dispatch code). Fixed
by building the `GraphExtraction` extractor explicitly in `ingest.py` and
passing `max_concurrency` to *its* constructor instead — that's the only
place it actually reaches the Step 1/Step 2 semaphores. Also fixed:
`--max-concurrency` now defaults per-backend (1 for ollama, 10 for azure)
instead of a flat default of 1.

## Azure backend: what's provisioned

Ran through `docs/azure-foundry-setup.md` steps 1–5. Live resources
(check `az account show` for which subscription is currently active if
picking this back up):

- Resource group: `rg-policy-system-graphrag-spike`
- Foundry resource: `policy-system-graphrag-spike`
  (`https://policy-system-graphrag-spike.openai.azure.com/`)
- Chat deployment: `gpt-5.4-mini` (`GlobalStandard` SKU, capacity 10) —
  **not** `gpt-4o-mini`, which is deprecated for new deployments as of
  2026-03-31 (confirmed via a real `ServiceModelDeprecated` error, held
  across every region checked).
- Embedding deployment: `text-embedding-3-large` (`Standard` SKU,
  capacity 10)

**Credentials are not saved anywhere persistent** (by design — see the
"no keys in files" conversation this session). Re-fetch before resuming:

```bash
export AZURE_API_KEY=$(az cognitiveservices account keys list \
  --name policy-system-graphrag-spike \
  --resource-group rg-policy-system-graphrag-spike \
  --query "key1" -o tsv)
export AZURE_API_BASE="https://policy-system-graphrag-spike.openai.azure.com/"
export AZURE_API_VERSION="2024-10-21"
```

**Rate limit is real and low**: the `gpt-5.4-mini` deployment's capacity
(10) is a **rate limit** (10 requests/60s), not a concurrency ceiling.
`--max-concurrency 10` (naively matching the number) caused a thundering
herd and real chunk loss (25/266 chunks permanently failed on the full
CRA run, ~9.4%). `--max-concurrency 3` reduced but did not eliminate this
— still lost chunks steadily throughout a full run. **Not yet fixed
properly** — needs either a real token-bucket client-side pacer (not
concurrency + retry-on-429) or a deployment capacity increase before a
full CRA/NIS2/GDPR run should be attempted again.

**Teardown** (not run): `az group delete --name
rg-policy-system-graphrag-spike --yes --no-wait`

## Bugs found and fixed (already committed to the working tree)

1. **`ingest.py`: `--max-concurrency` no-op for single-source ingest.**
   See "Ollama backend findings" above. Fixed.
2. **`ingest.py`: no logging handler configured.** `graphrag_sdk` logs
   pipeline step progress via the stdlib `logging` module but never
   configures a handler; without `logging.basicConfig(...)`, all of it
   was silently dropped. Fixed — also added `%(levelname)s` to the format
   string after initially omitting it caused real WARNING-level messages
   (chunk-level failures) to blend in with INFO noise and get missed by a
   grep filter.
3. **`compare.py`: node/edge type counting was wrong for GraphRAG-SDK
   graphs.** Every extracted entity carries *two* labels —
   `(:<Type>:__Entity__)` — with `<Type>` NOT reliably `labels(n)[0]`
   (observed both `['Regulation', '__Entity__']` and `['__Entity__',
   'Role']` in the same graph). Relationships are similarly generic:
   every domain relation is written as `:RELATES` with the real semantic
   type on an `r.rel_type` property (confirmed against
   `graph_extraction.py`'s `_relations_to_relationships`, which hardcodes
   `type="RELATES"`). Fixed with `CASE WHEN '__Entity__' IN labels(n)
   THEN n.type ELSE labels(n)[0] END` (and the `RELATES`/`rel_type`
   equivalent) — **note this is deliberately NOT a blind
   `coalesce(n.type, labels(n)[0])`**: the baseline pipeline's graph
   (`policy_system`) uses `type` as an unrelated *domain* property on
   some node types (e.g. `Capability.type = "technical"/"organizational"`,
   `Control.type = "manual"/"automated"`) — a naive coalesce broke the
   baseline graph's counts (`Capability`/`Requirement` both showed as 0)
   until gated specifically on the `__Entity__` marker.

## Full CRA run results (via Azure, `max_concurrency=3`)

Graph: `policy_system_graphrag_spike_azure` (also has `engprac` in it from
an earlier run — same graph, additive, for eventual cross-regulation
convergence testing).

Extraction: **1142 nodes, 2150 relationships extracted**, 25/266 chunks
lost to rate limiting (~9.4%). Wall time ~1.5h (mostly rate-limit retry
churn, not actual compute — a 14.5KB excerpt with no rate limiting takes
~45-90s end to end).

**Entity counts came out *higher* than the baseline pipeline's graph, not
lower** — `Regulation=145` (vs. baseline's 4), `Role=182` (vs. 19). Two
compounding causes:
- CRA's legal text cross-references dozens of *other* EU
  regulations/directives by name; the extractor has no concept of "only
  extract entities for the regulation you were actually handed" and
  faithfully creates a `Regulation` node for every citation.
- GraphRAG-SDK's default resolver (`ExactMatchResolution`) only merges
  entities on exact `(normalized_name, type)` match — no near-duplicate
  handling. Confirmed directly: `'Recommendation 2003/361/EC'` and
  `'Commission Recommendation 2003/361/EC'` (same real-world document)
  stayed as two separate nodes.

**71% of extracted relationships (1533 of 2150) pruned** for
`(source,target)` pattern mismatches against the ontology. Biggest
contributors: `HAS` (475 pruned), `SATISFIED_BY` (357), `EXPRESSES`
(304), `REQUIRES` (194). Two distinct failure shapes within this, not
one:
- **True direction inversion** — e.g. `MITIGATED_BY` declared
  `RiskPath→Capability`, model produced `Capability→RiskPath` (reads more
  naturally in English: "a capability mitigates a risk").
- **Hub-skipping / shortcut edges** — the model treats `Regulation` (and
  to a lesser extent `Role`) as directly connected to almost anything
  (`Regulation→Capability`, `Regulation→PracticeArea`) instead of
  following the intended multi-hop chain
  (`Regulation→Requirement→Obligation→Capability`).

## Root cause (best guess, not fully verified against `tools/graph-ingestion`'s actual source)

GraphRAG-SDK is a generic, schema-agnostic extraction framework pointed
at a domain-specific ontology it has no purpose-built understanding of —
it takes `schema.py`'s `Ontology` as prompt *content* (type descriptions
+ a bare list of declared patterns), not as an enforced specification.
The baseline pipeline was hand-built around `ps-domain-concepts.md`'s
specific conventions (deliberate scoping, a custom ID scheme forcing
convergence, presumably direction-explicit prompting) — GraphRAG-SDK has
none of that by default. Every symptom above traces back to this same
gap. **Not verified** — would need to actually read
`tools/graph-ingestion`'s source to confirm it does what's assumed here.

## Fix experiments (all on a fixed 14.5KB CRA excerpt — `docs/regulations/CRA.md` lines 60–250, heavy with regulation cross-references — chosen specifically to exercise the entity-proliferation problem without a full 266-chunk run)

Reproduction: the excerpt is at (session-scratchpad, **not persisted** —
regenerate via `"".join(open("docs/regulations/CRA.md").readlines()[59:250])`
if resuming) and was fed via `rag.ingest(text=..., document_id="CRA-EXCERPT-TEST")`.

| Variant | Total relationships pruned | relationships_created | Notes |
|---|---|---|---|
| Original schema, default resolver | 61 | 95 | Baseline |
| `SemanticResolution` (embedder, threshold 0.90) | — | — | Did not merge the known duplicate pair (`Recommendation 2003/361/EC` variants) |
| `LLMVerifiedResolution` (soft=0.80, hard=0.95) | — | — | Duplicate pair *appeared* merged, but confounded — resolver's own verification calls hit the same rate limit and failed for many pairs (fail-open = no merge on failure); can't attribute cleanly given extraction variance too |
| Direction-explicit relation descriptions only | **38** | 128 | **Best result found.** `DEFINES`, `MITIGATED_BY`, `REQUIRES` went to 0 pruning. `EXPRESSES`/`HAS` (the hub-skipping ones) improved but not eliminated (26, 11) |
| Direction-explicit + anti-shortcut entity descriptions | 49 | 126 | **Worse than direction-only.** `DEFINES` (8) and `REQUIRES` (11) pruning *came back* after being eliminated. Guess: more instruction text diluted/competed with the working direction guidance rather than reinforcing it. Not conclusively causal — see variance caveat |

### ⚠️ Current `schema.py` state

The file currently has **both** the direction-explicit fix (good, keep)
**and** the anti-shortcut additions to `Regulation`/`Role` descriptions
(the worse-performing variant, 49 vs 38 pruned). Left as-is deliberately
rather than auto-reverted — if picking this back up, either:
- Revert just the anti-shortcut paragraphs (the "IMPORTANT: only create a
  Regulation entity for..." / "A Role only ever relates directly to an
  Obligation..." additions) to get back to the known-better 38-pruned
  configuration, or
- Test the anti-shortcut idea again with each entity's addition isolated
  separately (Regulation-only vs Role-only) before concluding it's a bad
  idea outright — see "Untested variables."

## LLMExtractor Step-1 swap (2026-08-14, via `docs/graphrag-sdk-configuration.md`)

`ingest.py` now passes `entity_extractor=LLMExtractor(llm)` to
`GraphExtraction` (previously unset → defaulted to `GLiNERExtractor`). This
is a **persisted code change**, not an ad-hoc scratch-script experiment like
the schema.py variants above.

Tested once against the same fixed CRA excerpt used for the schema.py A/B
table (`docs/regulations/CRA.md` lines 60–250, `document_id=
"CRA-EXCERPT-TEST-LLMEXTRACTOR"`, isolated graph `llmextractor_test`),
current schema.py state (direction-explicit + anti-shortcut, the 49-pruned
baseline), Azure `gpt-5.4-mini`, `max_concurrency=3`:

- **Result: 59 of 77 raw relationships pruned (~77%)** — nominally worse
  than the 49/175 (~28%) GLiNER-default baseline on the same schema.
- **Not a clean comparison.** Two confounds, both real:
  1. Chunk 0's Step 2 call failed outright after 3 retries (Azure rate
     limit) and contributed zero relationships — shrinks the raw-extraction
     denominator in a way unrelated to extractor choice.
  2. `LLMExtractor` adds a second LLM call per chunk (Step 1 now hits the
     LLM too), roughly doubling request volume against the same
     `max_concurrency=3` Azure deployment whose rate limit was already
     "not yet fixed properly" per this file's own Azure section above. The
     run hit that limit repeatedly (visible in raw log: multiple
     `RateLimitError` retries/backoffs before the chunk 0 hard-failure).
  3. n=1 — the variance caveat from "Untested variables" #9 below applies
     here too.
- Prune-warning detail (now visible for free via the pipeline's built-in
  structured logging, exactly as `docs/graphrag-sdk-configuration.md`
  §2.5 predicted): the dominant pruned type was `EXPRESSES` (30 of 59,
  virtually all `(Regulation, Regulation)` pairs) — consistent with the
  known entity-proliferation problem (CRA's cross-references to other
  regulations becoming spurious `Regulation` nodes), not obviously a new
  failure mode introduced by `LLMExtractor` itself.

**Verdict: inconclusive, not negative.** The confound (doubled call volume
hitting an already-fragile rate limit) needs to be separated from the
actual variable (does LLM-driven Step 1 change extraction quality) before
this can be read either way. Re-running requires either explicit
client-side rate pacing (still not built — see Azure section above) or a
smaller/fewer-chunk excerpt to keep total calls under the limit. Not
re-run again immediately — each attempt is real billed Azure usage.
Deliberately left as a decision point for whoever picks this back up rather
than auto-retried.

## Untested variables (ranked by effort, cheapest first)

1. **Isolate `Regulation`-only vs `Role`-only anti-shortcut text** — the
   failed test changed both at once, confounding which one (if either)
   caused the regression.
2. **`LLMVerifiedResolution` with `max_llm_concurrency` explicitly set
   low** (1–2) — retest without the rate-limit confound from its own
   uncontrolled verification-call concurrency.
3. **Mechanical `Regulation` node filtering post-ingestion** — instead of
   relying on prompt compliance, just filter to keep only `Regulation`
   nodes matching the actual ingested document's known title/ID.
   Deterministic, doesn't depend on the model at all — probably the
   single most reliable fix on this list.
4. **Full worked examples instead of per-relation snippets** — show the
   *whole* chain (`Regulation→Requirement→Obligation→Capability`)
   composed together in 1-2 complete examples, not just isolated
   per-relation direction hints.
5. **Different chat model** — only `gpt-5.4-mini` tested on Azure. Is
   the residual pruning partly a capability ceiling (would a full `gpt-5`
   do meaningfully better with zero prompt changes), not just a
   prompt-clarity problem?
6. **Chunk size** — SDK default chunker used throughout (512 tokens, 2
   sentence overlap), never varied. Larger chunks might reduce
   hub-skipping (more context makes the compositional chain visible in
   one window); smaller chunks might reduce cross-type conflation.
7. **GLiNER's Step-1 confidence threshold** — Step 2 only verifies/extends
   what Step 1 already found; untested whether GLiNER's own threshold is
   a limiting factor.
8. **Deterministic relationship "repair" instead of pruning** — when a
   mismatched pair is found, check whether a valid intermediate entity
   already exists in the same chunk and rewire through it instead of
   discarding the fact. Bigger effort (custom pruning-stage code), but
   sidesteps prompt-following reliability entirely.
9. **Run-to-run variance control** — every result above is n=1. Raw
   extraction counts wobbled 64–76 nodes across otherwise-identical runs
   (same text, same schema, temperature=0). Before trusting any
   comparison in this doc as solid, running each config 2-3× and
   averaging would separate real effects from noise. Most important item
   on this list methodologically, even though it's not a "fix" itself.

## Graphs currently in FalkorDB

- `policy_system` — baseline, untouched by this spike.
- `policy_system_graphrag_spike` — early Ollama tests (gemma4, phi3).
  Contains partial/failed-extraction data from a killed first run mixed
  with a completed second run — not clean, don't treat as authoritative.
- `policy_system_graphrag_spike_phi3` — clean single-source (`engprac`)
  phi3 test, 0 relationships (the reasoning/async-garbling issue).
- `policy_system_graphrag_spike_azure` — **the real one.** `engprac` +
  full `CRA` via Azure, `gpt-5.4-mini`. This is what `compare.py` should
  target for any real comparison work.
- `dedup_test_baseline`, `dedup_test_semantic`, `dedup_test_llmverified`,
  `direction_test`, `antishortcut_test`, `llmextractor_test` — small isolated
  test graphs from the fix experiments above, all on the same 14.5KB CRA
  excerpt. Fine to drop if disk space matters; kept for now in case a fix
  experiment needs re-checking without re-running.

## Not yet done

- NIS2, GDPR sources never ingested via Azure (only `engprac` + `CRA`) —
  needed for a real cross-regulation convergence comparison against the
  baseline's 22 converged capabilities (current Azure graph shows only 1,
  but that's expected/not-yet-meaningful with only 2 of 4 sources in).
- Content-fidelity spot-checks (comparison dimension 2 from the README)
  — never done a systematic spot-check of extracted text against source
  articles, only ad-hoc reads during debugging.
- Falsification-step compatibility (comparison dimension 4) — never
  tested `tools/skills/falsification-step.md` against
  `policy_system_graphrag_spike_azure`.
- Clean (unconfounded) re-test of the `LLMExtractor` Step-1 swap — needs
  client-side rate pacing or a smaller excerpt so doubled call volume
  doesn't itself trigger rate-limit chunk loss. See "LLMExtractor Step-1
  swap" above.
- Remaining `docs/graphrag-sdk-configuration.md` recommendations #2–#5
  (resolver-concurrency fix, re-test `SemanticResolution` at an explicit
  lower threshold, switch to `.md` + `MarkdownLoader` + `StructuralChunking`,
  revisit anti-shortcut prompt wording) — not started; #1 (this file's
  `LLMExtractor` swap) was the only one implemented so far, per explicit
  scope decision to test one variable at a time.
