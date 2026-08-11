---
metadata:
  author: Cartman ApS
  copyright: "© 2026 Cartman ApS. All rights reserved."
  version: "0.1.0"
---

# Business Challenge Scoring Methodology

This instruction file defines the multi-perspective scoring framework for Business Challenge rubric evaluations. It covers only the mechanics of evaluation — workflow decisions (when to score, what to do with results) belong in skill files.

All rubrics use a 3-tier scoring system:
- **Pass (2)**: Criterion fully satisfied with clear evidence
- **Partial (1)**: Criterion partially met or evidence is ambiguous
- **Fail (0)**: Criterion not met or no evidence

Each field has a **Pass Gate** — a logical expression over its criteria (e.g., `C-001 Pass AND (C-002 Pass or Partial)`). A field passes only if its Pass Gate evaluates true. A Business Challenge passes only if ALL fields pass.

- If a field fails its Pass Gate, output must include a risk note.

**Conservative Interpretation**: When evidence is unclear, score Partial or Fail, not Pass.

---

## Why Three Perspectives

A single evaluator tends to anchor on their own expertise, missing blind spots that surface when forced to adopt unfamiliar viewpoints. Scoring from three distinct perspectives:

1. **Forces deeper reflection** — The model must reason about the same criterion through different lenses
2. **Surfaces hidden tensions** — Divergent scores reveal unresolved conflicts early
3. **Reduces confirmation bias** — Harder to justify weak content when it must pass three gates

---

## Perspective Definitions

| Perspective | Role Archetype | Primary Concerns |
|-------------|----------------|------------------|
| **Business** | Business owner, P&L accountable | Commercial value, customer impact, financial realism, ROI |
| **Strategic** | Strategy lead, cascade steward | Traceability to parent SI, portfolio coherence, horizon fit |
| **Functional** | Delivery lead, operations | Execution feasibility, measurability, resource and timeline realism |

---

## How Perspectives Interpret Business Challenge Criteria

The same criterion looks different through each lens:

| Criterion Type | Business Asks | Strategic Asks | Functional Asks |
|----------------|---------------|----------------|-----------------|
| **Objective (Outcome-First)** | Does achieving this change anything commercially meaningful? | Does this outcome advance the parent SI's End-State? | Is the outcome distinguishable from the activity used to pursue it? |
| **Measurability** | Is the target magnitude commercially significant? | Does the timeframe fit the planning cycle of the parent SI? | Is the baseline available? Is the target independently verifiable? |
| **Cascade Traceability** | Does this solve a real business problem? | Can you draw a direct line to the parent SI? | Is the scope clear enough to plan delivery against? |
| **Stretch Balance** | Will this change competitive position or customer outcomes? | Is this the right sized bet for this phase of the cascade? | Can delivery realistically achieve this within stated constraints? |
| **Anti-goals** | What commercial harms or gaming risks are unguarded? | What portfolio misalignments could this create? | What operational shortcuts could violate these boundaries? |
| **Main Effort** | Does this represent the highest-leverage investment? | Does this main effort directly serve the SI's strategic priorities? | Is this decomposable into work packages with clear ownership? |
| **Key Results** | Are these KRs commercially meaningful signals of progress? | Do the KRs together prove the objective was achieved? | Are leading indicators present alongside lagging ones? |
| **Constraints** | Are financial and market constraints realistic? | Do constraints align with SI-level resource allocation decisions? | Are constraints explicit, non-contradictory, and operable? |

---

## Scoring Scale

All rubrics use a three-level scale with both descriptive labels and numeric values:

| Label | Value | Meaning |
|-------|-------|---------|
| Pass | 2 | Criterion fully satisfied with explicit evidence |
| Partial | 1 | One sub-element missing but mitigated or acknowledged |
| Fail | 0 | Criterion not satisfied or no supporting evidence |

Use numeric values when rubrics specify thresholds (e.g., "total score ≥ 10/16").

---

## Resolving Conditional Gates

Some rubrics include conditional criteria (e.g., "if objective is high-stakes"). Resolve these before scoring:

**High-stakes**: An objective is high-stakes if ANY apply: involves safety, has regulatory/compliance implications, targets a gameable metric, involves cross-unit resource reallocation, or has >5% P&L impact on the unit.

---

## Scoring Process

### Step 1: Score Independently

Each perspective scores all criteria in the relevant rubric **without seeing other scores**.

### Step 2: Record Divergences

After all three perspectives have scored, note where scores differ:
- **Minor divergence**: 1 level apart (e.g., Pass vs Partial)
- **Major divergence**: 2+ levels apart (e.g., Pass vs Fail)

### Step 3: Calculate Composite

1. Convert each perspective's scores to numeric (Pass=2, Partial=1, Fail=0)
2. For each criterion: if two or more perspectives agree, take that label. If all three differ, score Partial and flag for resolution.
3. Sum numeric values when rubric pass gates require thresholds

### Step 4: Document Divergences

For each major divergence, record:
1. Which perspectives diverged and by how much
2. What each perspective saw that the others missed
3. Whether the divergence reflects artifact weakness or perspective-specific interpretation

---

## Scoring Output Format

Present scores in this structure:

```
## Scoring Summary

### Per-Perspective Scores

| Field | Business | Strategic | Functional | Composite |
|-------|----------|-----------|------------|-----------|
| Title | [score] | [score] | [score] | [score] |
| Description | [score] | [score] | [score] | [score] |
| Objective | [score] | [score] | [score] | [score] |
| Anti-goals | [score] | [score] | [score] | [score] |
| Main Effort | [score] | [score] | [score] | [score] |
| Key Results | [score] | [score] | [score] | [score] |
| Constraints | [score] | [score] | [score] | [score] |

### Divergences

| Field | Criterion | Divergence | Notes |
|-------|-----------|------------|-------|
| [Field] | [Criterion ID] | [e.g., Business:Pass, Functional:Fail] | [What was seen differently] |

### Pass Gate Status

| Gate | Status | Evidence |
|------|--------|----------|
| [Gate from rubric] | Pass/Fail | [Citation or reason] |
```

## Scoring Output Template (Normative)

Full Evaluation Output (Required)

The evaluator MUST return this shape conceptually:

1. Artifact
2. Field[]
3. Criterion[]
4. PerspectiveScore[]
5. Composite
6. FieldGateResult

Required content by level:

| Level | Required fields |
|-------|------------------|
| Artifact | artifact type, artifact id/version (or reference), rubric id/version, methodology version |
| Field | field id, field name, field gate expression |
| Criterion | criterion id, criterion text |
| PerspectiveScore | perspective, score label, score value, reasoning, evidence[] |
| Evidence item | source path, quoted text, optional line range |
| Composite | composite label/value, resolution rule used, divergence flag |
| FieldGateResult | gate status, gate evaluation explanation |

Perspective scoring requirements:

1. Every criterion is scored independently by all three perspectives.
2. Each perspective must provide explicit reasoning.
3. Each perspective must provide at least one evidence citation.
4. If evidence is missing, perspective score defaults to Fail.

### Divergence Recording Requirements

For every major divergence (2+ level difference between any two perspectives), include:

1. Diverging perspectives and labels
2. What was seen differently
3. Whether divergence indicates artifact weakness or interpretation variance

### Validation Rules for Output Acceptance

Reject evaluation output if any of these fail:

1. Any rubric criterion missing from any perspective.
2. Any perspective entry missing reasoning.
3. Any Pass or Partial score missing evidence.
4. Composite score missing for any criterion.
5. Field gate result missing for any rubric field.

---

## Scoring Guardrails

Apply these rules on every scoring pass without exception.

1. **Default to Fail.** If a criterion is not explicitly supported by quoted evidence, score Fail. Do not infer or extrapolate.
2. **Pass requires explicit textual match.** Citation must prove criterion satisfaction.
3. **Partial is narrow.** Allow only when exactly one sub-element is missing with named mitigation.
4. **No downstream backfilling.** Score each field/artifact on its own text.
5. **Conservative tie-breaker.** Pass vs Partial → Partial. Partial vs Fail → Fail.
6. **Perspective integrity.** Do not blend perspectives mid-evaluation. Complete one perspective fully before switching.
7. **Absent field rule.** If a field is entirely absent from the artifact, score all its criteria Fail (0).
8. **Cascade traceability check.** Before finalising the Objective composite score, verify that the Objective traces to the parent SI's End-State. If no parent SI is available, flag as a risk note — do not auto-pass.
