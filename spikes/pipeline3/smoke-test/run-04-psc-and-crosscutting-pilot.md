<!-- © 2026 Cartman ApS. All rights reserved. -->
# Pilot Run 4 — Policy/Standard/Control Sweep + RiskPath/Role Cross-Cutting

Fourth falsification pilot round for [README.md](../README.md), scoped per
[PROGRESS.md](../PROGRESS.md) D8. Targets the two gaps `run-03`'s Next
action flagged as new and thinly-tested: (2) a systematic sweep of the
Policy/Standard/Control layer on its own terms, not just as a byproduct of
Capability-convergence questions; and (3) the RiskPath/Role cross-cutting
nuance `run-03` NHQ3-4 surfaced (native/shared boundary is clean at
Policy/PracticeArea but not at RiskPath/Role) as a candidate falsification
angle in its own right.

Same two-part structure as `run-02`/`run-03`: natural-hard self-play
questions, then a seeded-defect set dispatched to isolated Agent
subagents blind to the defect.

## Structural fact-check (before question design)

Queried directly:

- **Policy/Standard/Control has no lifecycle-status variety to speak of**:
  all 10 Policies are `approved` (no draft/deprecated); Standards and
  Controls split 8 `implemented` / 2 `reviewed` (no draft/deprecated/
  planned anywhere); no Control's `next_review_date` is overdue relative
  to 2026-08-13. This itself narrowed what a "systematic sweep" could
  actually test — several originally-imagined angles (draft Policies,
  deprecated Standards, overdue reviews) have no real data to exercise,
  so the questions below test what *does* vary instead of inventing
  variety that isn't there.
- **The entire Capability→Policy→Standard→Control chain is strictly
  1:1:1:1 across all 10 chains** in this graph — every Policy governs
  exactly 1 Capability and supports exactly 1 Standard, every Standard is
  implemented by exactly 1 Control. `ps-domain-concepts.md`'s own worked
  example (one Policy governing multiple Capabilities, supporting 2
  Standards) is illustrative only — currently unrepresented in seed data,
  the same kind of "documented design, not-yet-seeded" gap `run-03` found
  for Capability convergence before `9baacf4`.
- **"Data Protection Officer" is two distinct Role nodes**
  (`role_data_protection_officer_44e087`, defined by GDPR-1.0; and
  `role_data_protection_officer_b5ac9e`, defined by ENGPRAC-3.0) with
  **zero** Capability-reach overlap (5 capabilities vs. 1, no shared
  member) — the clearest concrete instance in this graph of the Role
  Identity design `ps-domain-concepts.md` describes ("roles that are
  semantically similar across different regulations remain distinct
  nodes").
- Of ENGPRAC-3.0's 6 Roles, exactly one (`role_sre_lead_5b2fd3`, SRE
  Lead) has Obligations reaching both a native-only Capability (Service
  Reliability Management) and a regulation-shared one (Security
  Logging) — confirmed exhaustively, not just spot-checked.
- `rp_secure_build_release_d93f8a` mitigates the most distinct
  Capabilities of any RiskPath (4), split 2 shared / 2 native-only —
  the widest cross-cutting RiskPath by Capability count.

## Part 1 — Natural-hard questions (self-play)

Self-play per pipeline2 D14's known-weaker-than-cross-agent-isolation
caveat — same limitation as prior rounds' natural-hard tier.

### Area 2 — Policy/Standard/Control sweep

**NHQ4-1 — Policy→Standard fan-out.** Does any Policy in this graph
support more than one Standard, the way the domain model's own worked
example does? **Entities:** Policy, Standard. **Cap:** 5.
**Answer:** No — every one of the 10 Policies supports exactly 1
Standard; the whole chain (Capability→Policy→Standard→Control) is
strictly 1:1:1:1 throughout, including at the Capability→Policy fan-in
step the worked example also illustrates. **Falsification: 5 attempts,
none landed** — (1) total node counts (10/10/10) miss on their own but
rule nothing out; (2) per-Standard inbound `SUPPORTED_BY` count
individually, all exactly 1, misses, rules out a 2:0-masking scenario;
(3) orphan-Standard check (0 inbound), none found, misses; (4) checked
for any `_v2`+ Standard id suggesting a second version was ever seeded,
none found, misses; (5) checked Capability→Policy fan-in directly (does
any Policy govern 2+ Capabilities even without 2+ Standards) — misses,
also strictly 1:1.

**NHQ4-2 — Standard/Control status-pairing.** Is there ever a
lifecycle-status mismatch between a Standard and the Control that
implements it? **Entities:** Standard, Control. **Cap:** 5.
**Answer:** No — all 10 pairs match exactly (8 implemented/implemented,
2 reviewed/reviewed). **Falsification: 5 attempts, none landed** — direct
pairing query; Policy-status/Standard-status join (all `approved` Policies
pair with only `implemented`/`reviewed` Standards, no drift); full
Control `implementation_status` distinct-value check (only 2 values exist
at all: `implemented`, `reviewed` — no `planned`/`deprecated` anywhere);
`last_test_date`/`execution_frequency` pattern check across all 10
Controls for a plausibility anomaly correlating with status, none found;
re-inspection of the 2 `reviewed` pairs individually for internal
consistency.

**NHQ4-3 — which capabilities are "reviewed," and does convergence
correlate?** Which Capabilities' governance chain is `reviewed` rather
than `implemented`, and does that set include an EU/ENGPRAC-converged
one? **Entities:** Capability, Policy, Standard, Control (+ Regulation/
Obligation for convergence). **Cap:** 5.
**Answer:** 2 Capabilities: Component Inventory & SBOM Management
(converged, CRA+ENGPRAC) and Service Reliability Management
(ENGPRAC-native). One of each — not a convergence-correlated pattern.
Note: `reviewed` is *later* in the `draft → implemented → reviewed →
deprecated` lifecycle than `implemented`, i.e. further along, not behind
— worth stating so the answer doesn't imply these two are lagging.
**Falsification: 5 attempts, none landed** — both Capabilities' own
`status` property checked (both `active`, not `deprecated`); full re-scan
of all 6 converged Capabilities' Standard status confirming exactly 1 is
`reviewed`; RiskPath `status` for the RiskPaths mitigating these two
(both `active`); `next_review_date` proximity check for staleness on the
2 `reviewed` Controls (neither overdue).

### Area 3 — RiskPath/Role cross-cutting

**NHQ4-4 — is "Data Protection Officer" one Role or two?** Is Data
Protection Officer a single Role, or multiple distinct Role nodes — and
if multiple, do their Obligations converge on any shared Capability?
**Entities:** Role, Regulation, Obligation, Capability. **Cap:** 1 (Role
is explicitly in the ingested-spine bucket).
**Answer:** Two distinct Role nodes, one per defining Regulation
(GDPR-1.0, ENGPRAC-3.0), per the model's deliberate Role-identity design.
Zero Capability-reach overlap (5 vs. 1, no shared member).
**Falsification: 1 attempt, none landed** — checked whether the two
roles' *disjoint* Capability sets nonetheless converge one layer down via
a shared Policy or a shared RiskPath (a different form of convergence
than direct Capability overlap) — missed, no shared Policy or RiskPath
either; the disjointness is total, not just Capability-level.

**NHQ4-5 — which ENGPRAC Role spans native and shared, and does RiskPath
mirror it?** Which ENGPRAC Role has Obligations reaching both a
native-only and a regulation-shared Capability — and does any RiskPath
mitigate that same pair? **Entities:** Role, Obligation, Capability,
RiskPath. **Cap:** 1.
**Answer:** `role_sre_lead_5b2fd3` (SRE Lead) is the only ENGPRAC Role
spanning both categories (Service Reliability Management [native] +
Security Logging [shared]). No RiskPath mitigates that same pair — the
RiskPaths touching each of those two Capabilities are disjoint from each
other, so Role-level and RiskPath-level cross-cutting don't align on this
pair even though both layers cross-cut *somewhere*.
**Falsification: 1 attempt, none landed** — exhaustive check across all 6
ENGPRAC Roles' Capability sets, classified native vs. shared directly
(not spot-checked) — confirms SRE Lead is uniquely the one with both
non-empty.

**NHQ4-6 — widest cross-cutting RiskPath.** Which RiskPath mitigates the
most distinct Capabilities, and does it span both native-only and shared
Capabilities? **Entities:** RiskPath, Capability. **Cap:** 1.
**Answer:** `rp_secure_build_release_d93f8a`, 4 Capabilities — 2 shared
(Secure Development Lifecycle, Access Control & Authentication), 2
native-only (Release and Change Control, Quality Gate Assurance).
**Falsification: 1 attempt, none landed** — checked raw row count vs.
`DISTINCT` count for this RiskPath's `MITIGATED_BY` edges (4 = 4),
ruling out the known FalkorDB duplicate-edge-inflation failure mode as
the source of the "4."

## Part 2 — Seeded-defect set (blind, isolated Agent subagents)

Same method as `run-02`/`run-03`: a genuine Q&A pair, each given exactly
one deliberate construction-step defect, ground truth logged privately
below *before* dispatch, handed to a separate isolated `Agent`-tool
subagent with no shared context and no hint the batch contained defects —
only `falsification-step.md`'s stated preconditions.

| # | Area | Defect type | Claimed | Ground truth | Visible in handed data? |
|---|---|---|---|---|---|
| SEED4-1 | 2 | Status misstatement | SBOM capability's Standard/Control both "implemented" | Both "reviewed" | Yes — handed data table showed `reviewed` explicitly; answer prose contradicted its own supporting table |
| SEED4-2 | 3 | Identity conflation across a non-convergent layer | "The Data Protection Officer role" (singular) requires 6 Capabilities | Two distinct Role nodes (GDPR, ENGPRAC), obligations merged by matching on `name` instead of node identity | No — retrieval query itself silently matched both nodes; required the falsifier to re-query by defining Regulation to expose the merge |
| SEED4-3 | 2+3 bridge | Overclaim | All 4 Capabilities `rp_secure_build_release_d93f8a` mitigates are EU-regulation/ENGPRAC-shared | 2 of 4 (Quality Gate Assurance, Release and Change Control) trace to ENGPRAC-3.0 only, no external link | No — handed data was just Capability id/name pairs with no provenance column; required an independent per-Capability provenance trace |

**Result: 3/3 landed, each on the first attempt** — sixth consecutive
first-attempt landing across two seeded rounds (`run-03`'s 3/3 + this
round's 3/3), now covering status-claim defects, cross-layer identity
conflation, and provenance overclaims, not just the count-confusion
types `run-02`/`run-03` exercised:

- **SEED4-1** — landed by re-querying the Standard/Control nodes directly
  for their literal `implementation_status`; found `reviewed`, not
  `implemented` as claimed.
- **SEED4-2** — landed by querying `Role` through its defining `Regulation`
  (rather than trusting the name-only match the original retrieval used),
  exposing that two distinct Role nodes were silently combined under one
  singular "the Data Protection Officer role" framing.
- **SEED4-3** — landed by tracing each of the 4 mitigated Capabilities'
  own provenance back to `Regulation` nodes independently, finding 2 of 4
  have no external-regulation link at all, contradicting the "all 4 are
  shared" claim.

All three subagents independently determined the correct attempt cap
(5 for the Policy/Standard/Control question, 1 for the two ingested-spine
questions), matching the invoking step's own determination in each case.

## Findings

1. **0/18 falsification attempts landed on real (non-seeded) questions**
   across both areas — 15 across 3 Policy/Standard/Control questions
   (including 2 running the full 5-attempt cap on genuinely novel angles
   each time), 3 across 3 RiskPath/Role questions (all correctly
   determined 1-attempt cap). No confirmation-theater signal: every
   attempt within a question used a mechanically distinct angle
   (inverse-direction cardinality checks, orphan-node checks, version-
   suffix checks, distinct-value-set checks, provenance re-derivation,
   downstream-convergence checks one layer past the direct claim).
2. **3/3 seeded defects landed on attempt 1 under blind isolation**,
   extending the now six-for-six first-attempt landing streak
   (`run-03` + `run-04`) to genuinely new defect *types* — a status-value
   misstatement, a cross-layer identity conflation (exploiting the Role
   concept's deliberate non-convergence design), and a partial-provenance
   overclaim — not just repeats of the count-confusion shapes `run-02`
   introduced.
3. **The Policy/Standard/Control layer turned out to have almost no
   internal variance to falsify against**: strict 1:1:1:1 fan-out
   throughout, only 2 status values in use, no lifecycle-stage spread.
   This is itself informative for D6: the 5-attempt cap's justification
   (customer-governed layer is "actively revised," most plausible place
   for a construction-step error) is about data *volatility*, not
   structural complexity — this pilot found the current seed data has
   low complexity at this layer, which doesn't by itself say anything
   about whether the cap is right, only that a systematic sweep here has
   a smaller structural surface to test than the ingested-spine layer
   did in `run-01`/`run-02`.
4. **New candidate for hardening, distinct from `run-03`'s RiskPath/Role
   finding**: SEED4-2's landing mechanism — matching a `Role` (or any
   node with a non-convergent identity design) by a display property like
   `name` instead of by node identity, silently merging two distinct
   entities — is a genuinely new falsification angle, not a restatement
   of `run-03`'s "RiskPath/Role cross-cut the boundary" finding. Worth
   flagging alongside NP-002/NP-005/NHQ-4/`run-03`'s RiskPath-Role nuance
   as a hardening candidate, still out of scope for this spike per
   README.md step 6.
5. No FalkorDB dialect gaps or the known multi-`DISTINCT` count bug
   triggered this round either (NHQ4-6's raw-vs-distinct check matched
   exactly) — fourth round running without reproducing it.
