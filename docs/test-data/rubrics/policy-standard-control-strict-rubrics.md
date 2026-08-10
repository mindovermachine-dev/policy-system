<!-- © 2026 Cartman ApS. All rights reserved. -->
# Strict Rubrics for Policy, Standard, and Control

**Status:** Draft  
**Purpose:** Quality gate for generating import-ready Policy System seed data.  
**Scope:** Generic (not specific to engineering practices only).  
**Mode:** Strict per-record acceptance with dataset-level coverage gates.

---

## 1. Evaluation Model

Each record is evaluated in two stages:
1. **Hard-Fail Checks** (binary): all must pass.
2. **Scored Checks** (0 or 1 each): must meet the threshold.

### Decision Rule (All Types)
A record is **ACCEPTED** only if:
1. `hard_fail_pass == true`
2. `score >= min_score_threshold`

Otherwise the record is **REJECTED** and must be excluded from import JSON.

### Common Threshold
- `min_score_threshold = 4` out of 5 scored checks.

---

## 2. Policy Rubric (Strict)

### 2.1 Hard-Fail Checks (all required)
1. **Identity present:** non-empty `id` and `title`.
2. **Enforced status:** `status == approved` (enforced-only baseline).
3. **Ownership present:** non-empty `owner_id`.
4. **Governance link exists:** at least one inbound `GOVERNED_BY` from `Capability`.
5. **Lifecycle anchor present:** non-empty `version`.

If any hard-fail check is false, reject immediately.

### 2.2 Scored Checks (0/1 each)
1. **Scope clarity:** policy describes in-scope and out-of-scope boundaries.
2. **Normative language:** contains enforceable terms (must, shall, required).
3. **Review cadence clarity:** includes review interval or trigger condition.
4. **Exception pathway:** exception/risk acceptance mechanism is explicit.
5. **Measurable intent:** policy states at least one measurable outcome.

### 2.3 Pass Threshold
- Accept if score is **4 or 5**.

---

## 3. Standard Rubric (Strict)

### 3.1 Hard-Fail Checks (all required)
1. **Identity present:** non-empty `id` and `title`.
2. **Valid status:** `implementation_status` is one of `implemented` or `reviewed`.
3. **Parent link exists:** exactly one inbound `SUPPORTED_BY` from `Policy`.
4. **Version present:** non-empty `version`.
5. **Actionability present:** content contains concrete implementation instructions.

If any hard-fail check is false, reject immediately.

### 3.2 Scored Checks (0/1 each)
1. **Procedure specificity:** steps are explicit enough to execute consistently.
2. **Role clarity:** implementer/reviewer responsibilities are explicit.
3. **Boundary clarity:** applicability boundaries (system, environment, data class) are stated.
4. **Verification readiness:** standard can be directly verified by one or more controls.
5. **Change traceability:** revision rationale or change marker is present.

### 3.3 Pass Threshold
- Accept if score is **4 or 5**.

---

## 4. Control Rubric (Strict)

### 4.1 Hard-Fail Checks (all required)
1. **Identity present:** non-empty `id` and `title`.
2. **Valid type:** `type` is `automated` or `manual`.
3. **Valid status:** `implementation_status` is `implemented` or `reviewed`.
4. **Parent link exists:** exactly one inbound `IMPLEMENTED_BY` from `Standard`.
5. **Review anchor present:** non-empty `next_review_date`.

If any hard-fail check is false, reject immediately.

### 4.2 Scored Checks (0/1 each)
1. **Pass/fail objectivity:** clear and testable success criteria are defined.
2. **Execution clarity:** method and trigger/frequency are explicit.
3. **Evidence quality:** `evidence_ref` points to verifiable evidence location.
4. **Ownership clarity:** responsible executor/reviewer is explicit.
5. **Risk alignment:** control objective clearly maps to at least one `RiskPath`.

### 4.3 Pass Threshold
- Accept if score is **4 or 5**.

---

## 5. Dataset-Level Strict Gates

Even if individual records pass, dataset export is **blocked** unless all gates pass:

1. Every active `PracticeArea` has at least one accepted `Policy` via `OWNS`.
2. Every active `RiskPath` has at least one accepted `Control` via `VERIFIED_BY`.
3. Every accepted `Policy` has at least one accepted `Standard`.
4. Every accepted `Standard` has at least one accepted `Control`.
5. Every accepted `Capability` is linked to at least one active `PracticeArea` and one active `RiskPath`.
6. No rejected record may appear in `nodes` or `edges` of import JSON.

---

## 6. Recommended Output Fields for Rubric Execution

For each evaluated record, emit:

- `entity_type` (`Policy` | `Standard` | `Control`)
- `entity_id`
- `hard_fail_results` (object of booleans)
- `hard_fail_pass` (boolean)
- `scored_results` (object of 0/1)
- `score` (integer)
- `min_score_threshold` (integer)
- `decision` (`ACCEPTED` | `REJECTED`)
- `rejection_reasons` (array of strings)

This supports deterministic gating and clear auditability.

---

## 7. Strict Mode Import Rule

1. Build candidates.
2. Evaluate with this rubric.
3. Drop all rejected records.
4. Recompute graph links after dropping records.
5. Apply dataset-level gates.
6. Export import JSON only if all dataset-level gates pass.

If any dataset-level gate fails, generation run status is `FAILED_STRICT_GATE`.
