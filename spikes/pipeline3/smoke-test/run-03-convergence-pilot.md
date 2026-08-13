<!-- © 2026 Cartman ApS. All rights reserved. -->
# Pilot Run 3 — EU-Regulation / Engineering-Practice Convergence

Third falsification pilot round for [README.md](../README.md), scoped per
[PROGRESS.md](../PROGRESS.md) D7. Triggered by commit `9baacf4`
("connected engineering practices with eu regulations in the graph"),
which merged 6 of ENGPRAC's 12 `Capability` nodes onto the existing
CRA/GDPR/NIS2 canonical `Capability` nodes they duplicated, instead of
leaving them as separate ENGPRAC-native nodes. Combines D6's still-open
gap (the Policy/Standard/Control 5-attempt cap was never empirically
exercised) with a new gap this merge introduces (Obligation fan-in at a
shared `Capability`, spanning the ingested spine and the customer-governed
layer) — per D7's decision to run one combined round rather than two
separate ones.

Two-part structure, same as `run-02-harder-pilot.md`: natural-hard
self-play questions, then a seeded-defect set dispatched to isolated
Agent subagents blind to the defect.

## Structural fact-check (before question design)

Queried directly, not assumed from the diff:

- 6 Capabilities now converge ENGPRAC with at least one external
  regulation: `cap_access_control_authentication_151816` (CRA+GDPR+NIS2+ENGPRAC,
  4-way — literally all 4 regulations in the graph), `cap_secure_development_lifecycle_9f3224`
  and `cap_vulnerability_management_55d0c4` (3-way each), `cap_component_inventory_sbom_management_b5223c`,
  `cap_data_protection_by_design_default_69e489`, `cap_security_logging_c4d9e2` (2-way each).
- `cap_security_logging_c4d9e2` (CRA+ENGPRAC) is the exact node
  `ps-domain-concepts.md`'s own Worked Example 2 / Convergence section
  names as the model's canonical illustration — this merge is the seed
  data catching up to what the model was always designed to produce, not
  a novel structure.
- 4 Capabilities remain ENGPRAC-native by design (confirmed in
  `capability_merges.json`'s "KEEP NATIVE" notes): `cap_policy_exception_governance_3cfd12`,
  `cap_quality_gate_assurance_10ab6e`, `cap_release_change_control_2ec781`,
  `cap_service_reliability_management_a3c0fe`.

## Part 1 — Natural-hard questions (self-play)

Self-play per pipeline2 D14's known-weaker-than-cross-agent-isolation
caveat — same limitation as `run-01`/`run-02`'s natural-hard tier applies
here too.

### NHQ3-1 — convergence/boundary count

**Question:** Of the 10 distinct Capabilities ENGPRAC-3.0's Obligations
require, how many are also required by at least one obligation from an
external EU regulation, and which are ENGPRAC-only?
**Entities/Edges:** Regulation, Requirement, Obligation, Capability —
`EXPRESSES`, `SATISFIED_BY`, `REQUIRES`. Ingested spine only.
**Attempt cap:** 1 (no Policy/Standard/Control involved).

**Answer:** 6/10 shared (Access Control & Authentication — CRA+GDPR+NIS2;
Secure Development Lifecycle — CRA+NIS2; Vulnerability Management —
CRA+NIS2; Component Inventory & SBOM Management — CRA; Data Protection by
Design & Default — GDPR; Security Logging — CRA). 4/10 ENGPRAC-only:
Policy and Exception Governance, Quality Gate Assurance, Release and
Change Control, Service Reliability Management.

**Falsification:** 1 attempt, none landed
  1. Raw existence check (independent of the aggregation query) for any
     external-sourced Obligation reaching the 4 claimed-native
     Capabilities — missed, no rows returned, confirms no hidden
     external convergence.

### NHQ3-2 — distinct-regulation vs. distinct-obligation count

**Question:** For Access Control & Authentication (the 4-way
convergence), how many distinct regulations require it vs. how many
total obligations require it?
**Entities/Edges:** Regulation, Requirement, Obligation, Capability.
Ingested spine only.
**Attempt cap:** 1.

**Answer:** 4 distinct regulations (CRA-1.0, GDPR-1.0, NIS2-1.0,
ENGPRAC-3.0), 8 distinct Obligations total (GDPR 2, NIS2 4, CRA 1,
ENGPRAC 1) — the regulation count and obligation count are different
numbers answering different questions, not a discrepancy.

**Falsification:** 1 attempt, none landed
  1. Checked total regulation count in the graph — missed, but
     **materially improves the answer**: there are exactly 4 regulations
     in the entire graph, so "4-way convergence" means *every* regulation
     in the graph requires this capability, not a cherry-picked subset —
     worth stating explicitly rather than leaving "4 distinct regulations"
     to sound like a partial count.

### NHQ3-3 — Policy/Standard/Control traceability through a converged capability

**Question:** For Security Logging (the CRA+ENGPRAC convergence
`ps-domain-concepts.md`'s own worked example describes), what's the
governing Policy's status, and is its Standard/Control chain fully
implemented?
**Entities/Edges:** Regulation, Requirement, Obligation, Capability,
Policy, Standard, Control. Policy/Standard/Control involved.
**Attempt cap:** 5.

**Answer:** Governed by "Logging Audit Retention" Policy (`approved`).
One Standard (`implemented`), one automated Control (`implemented`,
`evidence_ref` populated, last tested 2026-08-06, next review
2026-09-06). Fully implemented end-to-end for both convergent sources.

**Falsification:** 5 attempts, none landed
  1. Checked for additional Standards under the Policy beyond the one the
     full-chain query surfaced — missed, only one Standard exists.
  2. Checked Control staleness via `last_test_date`/`next_review_date`
     despite `implementation_status = implemented` — missed, dates are
     current (daily frequency, tested a week before this pilot).
  3. Cross-checked the RiskPath `MITIGATED_BY` this Capability against its
     `VERIFIED_BY` Controls — missed as a contradiction, but **materially
     improves the answer**: the RiskPath verifies through two Controls
     from two *different* Policy/Standard chains (this one, plus
     `pol_engineering_policy_governance`'s), confirming RiskPath is
     genuinely cross-cutting rather than 1:1 with a single capability's
     governance chain — expected per the domain model, but worth stating
     so the answer doesn't imply a tighter coupling than exists.
  4. Checked both source Requirements' (CRA, ENGPRAC) `status` for
     independent deprecation — missed, both `active`.
  5. Checked the Control's `evidence_ref` for a real (non-empty) pointer
     rather than an unbacked `implemented` claim — missed, populated.

### NHQ3-4 — is the native/shared boundary clean one layer up?

**Question:** Do any of the 4 ENGPRAC-native-only Capabilities share a
governing Policy with one of the 6 regulation-shared Capabilities — is
the native/shared boundary clean at the Policy layer too, or does it blur
one layer up?
**Entities/Edges:** Capability, Policy (`GOVERNED_BY`).
**Attempt cap:** 5.

**Answer:** No — clean at Policy and PracticeArea. Each of the 4
native-only Capabilities' governing Policy governs only that one
Capability; none of these 4 Policies also governs a shared Capability.
Same holds one layer sideways at PracticeArea (`COVERS`). This does
**not** extend to RiskPath or Role, which intentionally cross-cut both
native and shared Capabilities — see attempts 3 and 5.

**Falsification:** 5 attempts, none landed against the Policy-boundary
claim; 2 attempts materially reshaped how the answer should be scoped
  1. Direct query: do any of the 4 native Policies also govern a shared
     Capability — missed, none of the 4 governs anything but its own
     native Capability (this is exhaustive given `Capability GOVERNED_BY`
     is many:1, so checking from the native side covers both directions).
  2. Same check one layer sideways via PracticeArea `COVERS` — missed,
     equally clean.
  3. Checked RiskPath `MITIGATED_BY` overlap between native and shared
     Capabilities — **found real overlap** (`rp_secure_build_release_d93f8a`
     mitigates both `cap_release_change_control_2ec781` [native] and
     `cap_secure_development_lifecycle_9f3224`/`cap_access_control_authentication_151816`
     [shared]; `rp_traceability_auditability_0de7fa` mitigates both
     `cap_policy_exception_governance_3cfd12` [native] and
     `cap_security_logging_c4d9e2` [shared]). Not a contradiction of the
     Policy-scoped claim, but it means an unscoped "the boundary is
     clean" would overclaim — RiskPath is deliberately cross-cutting per
     the domain model, so this is expected, not a defect, but it changes
     what the answer is allowed to assert.
  4. Sanity-checked attempt 2's query construction by listing native
     PracticeAreas' full `COVERS` set directly, unfiltered — missed,
     confirms attempt 2 wasn't a false negative from a query bug.
  5. Checked whether native Capabilities' Obligations share a `Role` with
     shared Capabilities' Obligations — **found overlap**
     (`role_sre_lead_5b2fd3` has Obligations reaching both a native and a
     shared Capability), same cross-cutting pattern as RiskPath, one layer
     lower.

## Part 2 — Seeded-defect set (blind, isolated Agent subagents)

Same method as `run-02`'s SEED-1..3: a genuine Q&A pair through this new
convergence structure, each given exactly one deliberate construction-step
defect, ground truth logged privately below *before* dispatch, handed to a
separate isolated `Agent`-tool subagent with no shared context and no hint
the batch contained defects — only `falsification-step.md`'s stated
preconditions.

| # | Defect type | Capability under test | Claimed | Ground truth | Visible in handed data? |
|---|---|---|---|---|---|
| SEED3-1 | Numeric misstatement | Access Control & Authentication | "totaling 6 distinct Obligations" | 8 (sum of the answer's own per-regulation breakdown: 1+1+2+4) | Yes — arithmetic check on the handed table |
| SEED3-2 | Incomplete-retrieval overclaim | Secure Development Lifecycle | "only CRA-1.0 requires it among external regulations" | NIS2-1.0 also requires it (2 Obligations, omitted from the retrieval handed over) | No — retrieval handed over was itself incomplete; required an independent query |
| SEED3-3 | Raw-row/distinct-count confusion | Vulnerability Management | "10 distinct regulations require it" | 3 distinct regulations (CRA, NIS2, ENGPRAC) — 10 was the Obligation-row count, not a regulation count | No — handed data was 10 Obligation rows with no regulation column; required an independent provenance query |

**Result: 3/3 landed, each on the first attempt**, under blind isolation,
mirroring `run-02`'s SEED-1..3 3/3 result exactly:

- **SEED3-1** — landed via a direct distinct-count query on `REQUIRES`
  (`total_distinct_obligations = 8`), contradicting the answer's stated 6
  and matching the sum of its own per-regulation breakdown.
- **SEED3-2** — landed via dropping the retrieval's `CRA-1.0` presupposition
  and querying which Regulations of any `source_type` reach the Capability
  through the full chain — found NIS2-1.0 also connects, contradicting the
  "only CRA-1.0" claim.
- **SEED3-3** — landed via tracing the full provenance chain back to actual
  `Regulation` nodes instead of trusting the Obligation-row count — found
  3 distinct regulations, not 10, correctly attributing the confusion to
  conflating Obligation-count with Regulation-count.

All three subagents independently determined attempt cap = 1 (correct —
none of the three questions' supplied Entities include Policy/Standard/
Control), matching the invoking-step's own cap determination.

## Findings

1. **0/12 falsification attempts landed on real (non-seeded) questions
   targeting the new convergence structure**, across a mix of 1-attempt
   and 5-attempt-cap questions, including one deliberately walking a
   converged Capability all the way through Policy/Standard/Control
   (NHQ3-3) — this is the first empirical exercise of the 5-attempt cap on
   real data, and it ran its full 5 attempts without a false positive.
2. **3/3 seeded defects landed on attempt 1 under blind isolation**,
   extending D5's 3/3 result to this specific new data shape — the
   falsifier's reliability against genuine defects is not shape-specific
   to what D4/D5 originally tested.
3. **Two non-landing attempts materially reshaped an answer's scope**
   rather than just confirming it: NHQ3-3 attempt 3 showed RiskPath
   verification is genuinely cross-cutting, not 1:1 with a single
   Capability's governance chain; NHQ3-4 attempts 3 and 5 showed the
   "clean boundary" finding holds at Policy and PracticeArea but
   explicitly does **not** hold at RiskPath or Role, which cross-cut by
   design. An answer that generalized "clean boundary" without that scope
   would have overclaimed. Same pattern D4/D5 already flagged
   (non-landing attempts still earning their cost) — now confirmed on a
   different structural feature.
4. **No confirmation-theater signal**: all 4 natural-hard questions'
   12 total attempts used mechanically distinct angles (independent
   distinct-count re-derivation, raw-existence sanity checks, staleness
   checks, cross-edge-type overlap checks, query-construction
   sanity-checks) — no repeated weak angle in different words, consistent
   with `run-01`/`run-02`.
5. **D6's scope-aware cap held up under its first real exercise**: the
   3 seeded-defect subagents and the invoking-step's own cap
   determination for all 4 natural-hard questions agreed independently
   every time — Policy/Standard/Control involvement correctly triggered
   the 5-cap, its absence correctly triggered the 1-cap, with no
   under- or over-triggering observed.
