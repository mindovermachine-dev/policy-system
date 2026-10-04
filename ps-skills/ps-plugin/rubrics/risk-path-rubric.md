<!-- © 2026 Cartman ApS. All rights reserved. -->

# RiskPath Authoring Rubric

**Status:** Source of truth
**Applies to:** `RiskPath` nodes (see `ps-domain-concepts.md#riskpath`)
**Scoring model:** See `scoring-model.md` for mechanics — this file lists
criteria only.
**Pass threshold:** 80 / 100

---

## Criteria

| ID     | Dimension                 | Weight | Pass (2)                                                                                                                           | Partial (1)                                                                                          | Fail (0)                                                                                             |
| ------ | ------------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| RP-001 | Risk Framing Clarity      | 0.25   | States a concrete adverse outcome being mitigated (e.g. "unauthorized code reaches production undetected"), not just a topic label | States a topic area with the adverse outcome only implied                                            | Pure topic label with no stated adverse outcome (e.g. just "Security")                               |
| RP-002 | Cross-Cutting Scope       | 0.20   | Genuinely cuts across multiple Capabilities and would plausibly be verified by more than one Control                               | Plausibly cross-cutting but the description reads like it's really about one specific control        | Describes a single, specific control mechanism rather than a risk exposure                           |
| RP-003 | Risk Type Categorization  | 0.15   | `risk_type` is set and accurately matches the described exposure                                                                   | `risk_type` is set but is a loose or debatable fit                                                   | `risk_type` is absent or clearly mismatched (e.g. `privacy` for a supply-chain exposure)             |
| RP-004 | Verification Traceability | 0.25   | Concrete enough that at least one real Control could plausibly satisfy `VERIFIED_BY` for it, as described                          | Verification is conceivable but would require significant interpretation to design a Control against | So abstract that no concrete Control could be designed to verify it                                  |
| RP-005 | Lifecycle Honesty         | 0.15   | `status` (`active`/`deprecated`) accurately reflects real current relevance                                                        | Status is plausible but unverifiable from content alone                                              | Status contradicts content (e.g. `deprecated` risk path still described as a required baseline path) |

**Weight sum check:** 0.25 + 0.20 + 0.15 + 0.25 + 0.15 = 1.00

---

## Rationale Notes

- This record type had no per-record criteria in the superseded rubric — it
  only appeared in dataset-level gates. RP-001/RP-002 exist so a RiskPath is
  scored as a genuine risk lens (per its own description in
  `ps-domain-concepts.md`) rather than a restated Capability or Control.
- The 6-required-risk-path completeness check (AC-BI-002) is a
  **dataset-level** gate, not a per-record criterion, same reasoning as
  Control's automation-ratio gate.
