<!-- © 2026 Cartman ApS. All rights reserved. -->
# Spike: End-to-End Slice

**Status:** Planned (depends on `cli-tool-semantics`)

---

## Purpose

Prove the full UC-3 path works end-to-end with real components: **one question, through the entire stack, answered faithfully.**

This is the integration test, not a learning vehicle — the learning happened in `skill-transfer` and `cli-tool-semantics`. This spike exists to catch integration surprises.

## Prerequisite

`cli-tool-semantics` must have produced a working CLI + skill combination.

## The Test

### Setup

1. **PS Subsystem as a service**: wrap the proven query machinery (v1 template router + Candidate D catalog + v2 agentic fallback) behind a real HTTP API — FastAPI, learning from c4b's `api/` layer patterns but single-tenant, no auth beyond localhost.

2. **PS API Gateway**: thin routing layer in front of the subsystem. In prototype mode this may be nearly transparent — its job is to exist as a named boundary so future concerns (auth, rate limiting, audit logging) have a home.

3. **PS CLI** from `cli-tool-semantics`, updated to call the API Gateway instead of wrapping libraries directly.

4. **Helvex graph** in FalkorDB, as before.

5. **Run the full question set** through the complete stack:
   - Development set from `skill-transfer` (~20 questions)
   - Held-out set from `skill-transfer` (~20 questions)
   - Each question flows: User → Harness (agent + skill) → PS CLI → API Gateway → PS Subsystem → FalkorDB → back with facts + provenance → synthesized answer

### Success Criteria

| Criterion | Threshold |
|---|---|
| **End-to-end correctness** | 50% on development set; 50% on held-out set (same thresholds as `skill-transfer`) |
| **Provenance completeness** | Every answer cites the full source chain (Regulation → article → Obligation → Capability → Policy → Standard → Control) |
| **Trust flags** | Stale/partial chains are labeled as such — not laundered into clean prose |
| **No component bypasses** | Every request flows through all four components (verified by logs/tracing at each hop) |
| **Held-out consistency** | Held-out results within 15% of development-set results — a large gap indicates integration overfitting |

### Failure Modes to Watch

- Integration impedance mismatches (CLI output format ≠ what the API expects; API response format ≠ what the agent can parse)
- Latency surprises (each hop adds overhead — is the total acceptable for interactive use?)
- Error propagation: what happens when FalkorDB is down, or the subsystem returns an error — does the agent get a useful message or a stack trace?
- The gateway is transparent enough that it adds no value yet — that's fine for prototype, but note it
- **Integration overfitting**: the full stack works for development questions but fails on held-out shapes — the blind-generated held-out set is the check

## What This Is NOT

- Not a production service — no auth, no TLS, no deployment packaging
- Not testing UC-1/UC-2/UC-4/UC-5 — those get their own end-to-end slices later
- Not performance testing — functional correctness only

## Deliverables

- A running PS Subsystem service (minimal FastAPI app)
- A running PS API Gateway (minimal routing layer)
- Updated PS CLI calling the gateway
- Results tables: development set and held-out set (~20 each), each with full-stack pass/fail + component traces
- A verdict: the four-component architecture carries UC-3 end-to-end, or specific integration issues to fix
