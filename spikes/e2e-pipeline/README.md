<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: E2E Pipeline (Live Answer Verification Loop)

**Status:** Proposed 2026-08-10, not yet implemented.

## Purpose

Build the live path for [AD-7](../../docs/architecture/ps-prototype-architecture.md)'s
PS Answer Verification Pipeline: a question asked in VS Code chat goes
harness → CLI → subsystem → verification pipeline → three-block annotated
answer, in the same turn. `compliance-decision-pipeline` already validated
Stage 1/2/4, routing, and the composer against 24 known scenarios; this
spike builds the adapter that lets that logic run against live, unseen
questions.

## Not `end-to-end-slice`

`spikes/end-to-end-slice` is a separate, still-unbuilt spike scoped to an
HTTP API Gateway (components 1–4), predating AD-7. Kept separate — no
overlap in goal.

## Not a shared import

This spike forks `compliance-decision-pipeline/pipeline/*` and
`cli-tool-semantics/ps.py` into its own tree instead of importing them
live — spikes don't share code across boundaries (see
[[spike-independence-no-shared-code]]). `compliance-decision-pipeline`
stays frozen as its own historical record.

## Gaps to close

1. **No CLI entrypoint.** `compliance-decision-pipeline` only has
   `tests/run_target_cases.py` (fixed scenarios). Needs a callable
   `pipeline query "<question>" --answer <claims>` command.
2. **No claim-extraction path.** Stage 4 checks take structured input
   (capability id, claimed count/status) currently hand-derived per test
   case. Resolved (D1): harness emits structured claims alongside prose
   (avoids reintroducing model non-determinism via prose-parsing) — one
   claim kind per Stage 4 check, schema in `PROGRESS.md`. Claim-schema
   adapter (dispatching a `ClaimSet` to fitness.py calls) still to build.
3. **No harness wiring.** A CLI existing doesn't make the agent call it —
   needs skill-level grounding per AD-6 — see D3.

Carried over unchanged, not this spike's scope unless a live question
forces it: routing-table blind spots (no mandatory check for unseen signal
combinations), Miscount tool-computed-count check, `stale_chain_strict_reading`,
judge ensemble/human escalation.

## Setup

1. Resolve D1 (done) / D3 (`PROGRESS.md`).
2. Fork `pipeline/*` and `ps.py` into this spike (D2 — done, plus `ps.py`'s
   real dependency closure, `query_mechanism_v1.py` + `catalog.py`).
3. Build the CLI entrypoint.
4. Build the claim-schema adapter.
5. Wire the harness side.
6. Run real, previously-unasked questions through it live.
7. Compare misses against the known failure-kind taxonomy — a new miss is
   a Stage-5-shaped finding, logged, not absorbed silently.

## Success Criteria

| Criterion | Threshold |
|---|---|
| Live round trip | Question in chat → three-block answer via the pipeline CLI, no manual step |
| Claim fidelity | Extracted claims match what the harness actually claimed |
| Routing visibility | (C) shows the routing decision for every live answer |
| No false auto-pass | Zero live answers marked confident/verified that are actually wrong |
| New-gap logging | Any uncaught failure kind is recorded explicitly, not swallowed |

## Failure Modes to Watch

- Claim-extraction mismatch (adapter records something other than what the harness claimed)
- Latency (live Stage 4 re-queries added to an interactive turn)
- Harness not actually calling the pipeline (grounding problem, not code)
- Scope creep toward `end-to-end-slice`'s HTTP gateway goal
- Routing-table blind spots surfacing live (inherited, not new)

## What This Is NOT

- Not a Stage 1/2/3/4 rebuild — forked from validated logic, expected to diverge
- Not an HTTP gateway — CLI keeps talking to FalkorDB directly
- Not closing `compliance-decision-pipeline`'s other open items unless a live question forces it
- Not building the judge ensemble or human escalation UI
- Not production — no auth, no deployment packaging

## Deliverables

- Pipeline CLI entrypoint
- Claim-schema adapter
- Harness-side skill wiring
- Log of real live questions run this session, with outputs and any new failure kind found
- AD-7 live-loop verdict
