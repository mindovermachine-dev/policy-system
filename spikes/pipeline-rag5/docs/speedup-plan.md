# © 2026 Cartman ApS. All rights reserved.
# Speedup plan for map_graph.py's LLM-curated cascade

Status: the full-cascade run this plan was deferred behind completed
2026-08-17 16:14 UTC (52.1 min, 0 errors, AC-3 PASS after a separate
DEFECT-1 property-naming fix applied post-run). This plan is the next
actionable item -- see "Empirical confirmation" below for real numbers from
that run, which changed the original Track A/B split.

## Context

The current cascade run is projected at ~25-40 min. Investigating why turned
up two separate, independently-actionable causes -- one a code gap, one a
quota ceiling -- confirmed against the actual Azure deployment
(`az cognitiveservices account deployment show ... gpt-5.4-mini`), not
assumed:

```
sku.capacity: 10
rateLimits: request=10 per 60s, token=10000 per 60s
```

The deployment is genuinely provisioned at 10 RPM / 10K TPM today -- not the
1000 RPM / 1M TPM / 100-concurrency figures floated in conversation. This is
the reasoning that motivated splitting the work into two tracks; see
"Empirical confirmation" below for how the actual run reshaped where the
track boundary sits:

1. **Code gap, fixable now, no quota change needed:** `RateLimitedLLM` is
   configured `concurrency=2`. At ~15-20s/call that yields ~6-7 calls/min --
   *below* the 10 RPM the deployment already allows. Stage 1's batching loop
   (`stage1_role_test`) is also a plain sequential `for batch in ...: await
   ...`, not `asyncio.gather` -- despite the original plan calling it
   "parallel, batched," it never used the concurrency headroom at all.
2. **Real ceiling, needs a deliberate quota change:** at ~1500-2000 tokens/call
   (small prompts, this workload), 10K TPM only sustains ~5-7 calls/min
   regardless of concurrency or RPM. TPM, not RPM, is the binding constraint
   today. Raising concurrency alone runs into it quickly. Getting materially
   faster requires raising `sku.capacity` on the deployment itself -- a real
   Azure infrastructure/cost change against a shared resource, not a code
   tweak.

## Empirical confirmation (2026-08-17, after the run this plan deferred behind)

The run this plan was written to speed up has since completed (52.1 min,
3118.8s, zero errors). Real numbers, not estimates:

- **Azure Monitor** (`az monitor metrics list` on the account, ground truth,
  not self-reported): 554,018 prompt tokens + 51,857 completion tokens =
  **605,875 total tokens**.
- **Log-derived call count** (gap-detection between decision-log timestamps,
  `role_test`/`req_obl`/`capability` stages): ~12 role_test calls + 1 dedup
  call + ~76 req_obl calls + ~83 capability calls ≈ **172 total LLM calls**.
- That's **~18.1s average latency/call** and **~3,522 avg tokens/call** --
  both higher than this plan's original back-of-envelope guess (~15-20s
  latency was about right; ~1500-2000 tokens/call was an underestimate).
- **Zero retry/throttle warnings** anywhere in the run's stdout (checked:
  no "retrying", "429", "RateLimit" -- litellm logs these explicitly on
  every retry, per `LiteLLM.ainvoke`'s source, so their total absence is a
  real negative result, not silence-by-omission).
- **`grep -n "asyncio.gather\|create_task" map_graph.py` returns nothing.**
  Confirmed directly, not inferred: concurrency was never exercised *at all*,
  in *any* of the four stages -- not just Stage 1's batching loop as
  originally flagged. Every stage awaits one call, then the next, in a plain
  loop. `RateLimitedLLM`'s `concurrency=2` semaphore never had a second
  caller to admit.

**This changes the diagnosis.** 172 calls sequentially at ~18.1s/call
accounts for the entire 3118.8s runtime almost exactly (172 × 18.1s ≈
3113s) -- there is no unexplained gap to attribute to Azure-side throttling.
And critically: achieved throughput (~3.3 calls/min, ~11,600 tokens/min
sustained over the full 52 minutes) ran *at or slightly above* the nominal
10,000 TPM figure with **zero throttling observed**. Either the published
number has burst tolerance/smoothing this workload's call spacing didn't
trip, or it's enforced more loosely than the flat per-60s figure implies.
Either way, the original "TPM is likely already saturated, Track A has
limited upside" framing doesn't hold up against what actually happened --
the run was 100% code-sequential-bound, not quota-bound. Track A's real
ceiling is unknown until concurrency is actually turned on and pushed until
a 429 is seen; it is not already known to be small.

## Track A -- fix the code within the current 10 RPM / 10K TPM quota (do this first)

No infra change, no confirmation needed beyond normal code review. Given the
empirical result above, this track now absorbs what the original version of
this plan had filed under Track B (items 1-3 below) -- that restructuring
was never actually dependent on a quota increase, only on removing
artificial sequentiality. Re-scoped:

- **Add token-aware gating first, before touching concurrency anywhere.**
  `RateLimitedLLM` currently only counts requests, not tokens (confirmed by
  reading `ratelimit.py`) -- it has no way to know it's approaching the 10K
  TPM budget once concurrency goes above 1. Add a lightweight token estimate
  (chars/4 is fine for this workload, no real tokenizer needed) and a second
  sliding-window gate alongside the existing request-count one, *inside
  `map_graph.py`* rather than editing the shared `ratelimit.py` (which
  `ingest.py` also depends on -- keep its contract stable). This is the
  safety net that makes it safe to find the real ceiling experimentally
  instead of guessing at one.
- **Make Stage 1 actually concurrent.** Replace the sequential
  `for batch in _chunked(...): await call_json(...)` loop in
  `stage1_role_test` with `asyncio.gather` over all batches at once.
- **Decouple Stage 2/3's Requirement+Obligation calls from role-order.** The
  only genuine cross-role dependency is the obligation text-collision check
  (`obligation_registry` in `stage2_3_requirements_obligations`) -- and
  that's a cheap string comparison (`_canonical_key`), not something that
  needs the *LLM call itself* serialized. Restructure to: run every kept
  role's Requirement+Obligation call concurrently (each is self-contained --
  a role's own chunk-scoped candidates), collect all results, *then* run one
  fast, LLM-free pass applying the `_canonical_key`/`"...as {Role}"`
  disambiguation across the full result set before writing to `final_full`.
  Same outcome, no forced serialization.
- **Restructure Stage 4 the same way.** Run Capability-matching calls
  concurrently in waves against a snapshot of the registry-so-far, then run
  one cheap merge pass at the end -- reuse the existing `ROLE_DEDUP_SYSTEM`
  pattern (list all newly-minted Capability names, one call, ask which are
  duplicates of the same underlying capacity) to fold together anything two
  parallel calls independently minted for the same capacity.
- **Push concurrency up incrementally, watching for real 429s** -- start at
  ~4, re-run `--limit 5`, check `logs/full-cascade-run.out` for litellm's
  own retry-warning lines (confirmed working: they log on every retry) and
  check the token gate isn't rejecting/stalling calls. Step up (6, 8...)
  until either a 429 actually appears or concurrency=~9 is reached (leaving
  headroom under the 10 RPM ceiling, which -- unlike TPM -- the deployment
  metadata says *is* a hard count). Record where it actually breaks, not
  where it's assumed to.
- Expected effect: unknown until measured -- that's the point of the
  incremental rollout above. The 605,875-token / 172-call run took 52 min
  doing that work item-by-item; if even 4-way concurrency holds without
  throttling, that's a real ~4x on the sequential portions (all of Stages
  2-4, which dominated this run's time).

## Track B -- raise the actual Azure quota (only if Track A's incremental rollout hits a real ceiling)

**Do not run the `az cognitiveservices account deployment update` (or
portal-equivalent) quota change as part of "implementing this plan."**
Surface it to the user as its own decision if/when Track A's concurrency
rollout above actually produces a 429 that token-gating and staying under
10 RPM can't route around -- it's a cost/infra change to a shared resource,
per the standing rule to confirm before actions like that. Track A already
absorbed the code changes that would let a raised quota matter; if this
track is reached, it's a pure numbers change (`sku.capacity` 10 -> e.g. 100)
plus widening Track A's token gate and `--role-batch-size`/`--req-batch-size`
to the new ceiling, not new code.

## Files touched

- `spikes/pipeline-rag5/map_graph.py` -- `stage1_role_test`,
  `stage2_3_requirements_obligations`, `stage4_capability` (all three made
  concurrent), plus a small token-estimate/gate helper.
- `spikes/pipeline-rag5/ratelimit.py` -- not touched; `ingest.py` depends on
  its current contract and won't be re-run, but there's no reason to widen
  its blast radius for a curation-only need.

## Verification

1. Re-run `--limit 5` first (same discipline as the last two rounds) to
   confirm the concurrent version doesn't 429 and produces the same
   kept/dropped decisions as the sequential version did on the same roles.
2. Step concurrency up per Track A's rollout plan, re-running `--limit 5`
   at each step, until a real ceiling is found or ~9 is reached safely.
3. Full re-run; compare wall-clock and total tokens/calls (Azure Monitor +
   log gap-detection, same method as the Empirical confirmation section
   above) against this run's 52.1 min / 605,875 tokens / 172 calls baseline.
4. Diff final Role/Requirement/Obligation/Capability counts (via
   `compare.py`) between this sequential run and the concurrent rerun on the
   same `native_full` -- should be close, not materially different, since
   the restructuring only changes *when* independent decisions happen, not
   what they decide. Any material divergence means the post-hoc merge passes
   aren't actually equivalent to the inline sequential checks and need
   revisiting before trusting the faster version.
