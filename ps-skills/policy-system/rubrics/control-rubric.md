<!-- © 2026 Cartman ApS. All rights reserved. -->

# Control Authoring Rubric

**Status:** Source of truth
**Applies to:** `Control` nodes (see `ps-domain-concepts.md#control`)
**Scoring model:** See `scoring-model.md` for mechanics — this file lists
criteria only.
**Pass threshold:** 80 / 100

---

## Criteria

| ID    | Dimension             | Weight | Pass (2)                                                                                                                                                                                                          | Partial (1)                                                                                                  | Fail (0)                                                                                                                |
| ----- | --------------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| C-001 | Pass/Fail Objectivity | 0.20   | Success criteria are defined precisely enough that two different executors would reach the same pass/fail verdict                                                                                                 | Success criteria exist but require judgment calls                                                            | No defined success criteria — "check that it works"                                                                     |
| C-002 | Execution Clarity     | 0.15   | `execution_method` describes the method and, if `execution_frequency` is not yet set (expected at authoring time for `planned` Controls), makes clear what the intended trigger or cadence will be                | `execution_method` describes the method but frequency/trigger intent is unclear even as a stated future plan | `execution_method` empty or no method described                                                                         |
| C-003 | Evidence Path Defined | 0.15   | `evidence_plan` makes clear what evidence this Control will produce and where it will live, even though `evidence_ref` is legitimately null at authoring time                                                     | `evidence_plan` gestures at evidence ("logs will be checked") without saying what artifact or location       | `evidence_plan` empty — no indication of what evidence this Control could ever produce                                  |
| C-004 | Ownership Clarity     | 0.15   | States who executes and who reviews the result, distinctly                                                                                                                                                        | States that ownership exists without naming or distinguishing roles                                          | No ownership information                                                                                                |
| C-005 | Risk Alignment        | 0.20   | Control's objective clearly and specifically maps to the risk exposure of the RiskPath(s) it will be `VERIFIED_BY`                                                                                                | Plausibly related to a RiskPath but the connection requires inference                                        | No discernible connection to any risk exposure                                                                          |
| C-006 | Lifecycle Honesty     | 0.15   | `implementation_status` accurately reflects real maturity — `planned` for newly authored/unexecuted Controls, or a later state supported by real evidence (e.g. `implemented` only with actual execution history) | Status is plausible but unverifiable from content alone                                                      | Status claims a maturity (`implemented`/`reviewed`) unsupported by any evidence, `last_test_date`, or execution history |

**Weight sum check:** 0.20 + 0.15 + 0.15 + 0.15 + 0.20 + 0.15 = 1.00

---

## Rationale Notes

- C-002 and C-003 replace the superseded rubric's "execution clarity" and
  "evidence quality" checks, which implicitly assumed non-null
  `execution_frequency`/`evidence_ref` — fields `ps-domain-concepts.md`
  states are "never populated by the adapter on mint" for internal-seed
  content. These criteria instead score the dedicated `execution_method`/
  `evidence_plan` properties' clarity about intended method/evidence, not
  whether the operational fields are already filled in.
- C-006 replaces the superseded rubric's `implementation_status ∈
{implemented, reviewed}` hard-fail and its `next_review_date` non-null
  hard-fail, both of which would have rejected every freshly minted
  Control (default `planned`, operational fields null by design).
- The 60–70% automated-Control target (AC-BI-009) is a **dataset-level**
  gate, not a per-record criterion — it can't be scored on a single Control
  in isolation. It stays a dataset-level check applied after per-record
  scoring, mirroring the superseded rubric's own dataset-gate section.
- `pass_fail_criteria`, `execution_method`, `evidence_plan`,
  `executor_role`, `reviewer_role`, and `risk_alignment_rationale` are real,
  dedicated properties on the `Control` node (see
  `ps-domain-concepts.md#control`), added specifically to pair 1:1 with
  these criteria and with `control-template.md`'s sections — not free-form
  content buried inside `description`, which is now a short summary field
  only.
