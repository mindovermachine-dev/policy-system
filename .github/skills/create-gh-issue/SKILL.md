---
name: create-gh-issue
description: >-
  Create tracked GitHub issues following the standard-mom issue template, with
  rigorous acceptance criteria derivation. Challenges intent upfront and derives
  testable AC from multiple perspectives.
metadata:
  author: platform
  version: "4.0.0"
  tags: [gh-issue, backlog-item, interactive]
  copyright: "© 2026 Cartman ApS. All rights reserved."
---

# Create GitHub Issue

## Purpose

Create tracked GitHub issues that follow the standard-mom issue template
(`.github/ISSUE_TEMPLATE/standard-mom.md`), with:

1. Validated intent (the "why" must be defensible)
2. Rigorous, testable acceptance criteria derived from multiple perspectives
3. Appropriate scope (split if too large)

## Inputs

- User input — what they want to achieve and why
- Optionally: an ADR or existing GitHub issue to derive context from
- Domain artifacts: Domain Concepts, User Requirements (Use Cases)
- `system-config.md` — for `github.project`, if issues should be added to a project board

## Outputs

- A tracked GitHub issue, created via `gh issue create`

---

## Phase 1: Challenge Intent

**Goal:** Ensure we're building the right thing before deriving AC for it.

### Step 1: Understand What

Ask: **"What is the problem, that needs to be solved?"**

Let the user describe the problem, need, or risk in concrete terms. If an ADR or
existing issue is referenced, read it and extract context instead of re-asking.

**Required output:** `Problem:` concrete description of the problem/challenge/risk

### Step 2: Socratic Challenge on Why

Ask **2-3 probing questions** to establish clear value or risk:

| User's Initial Why         | Follow-up Probe                                                    |
| -------------------------- | ------------------------------------------------------------------ |
| "Stakeholder requested it" | Who benefits? What problem does this solve for them?               |
| "Technical debt"           | What user-visible symptom does this cause? Cost of not fixing?     |
| "Compliance"               | Which regulation? What's the penalty or risk of non-compliance?    |
| "Performance"              | Who experiences the slowness? What's the impact on their workflow? |
| "Security"                 | What's the threat scenario? What's the blast radius if exploited?  |

### Step 3: Gate

**Do not proceed** if the "why" remains weak after probing.

Weak "why" indicators:

- Circular reasoning ("we need it because we need it")
- Authority appeal without impact ("boss wants it")
- Vague benefit ("it would be nice")

If weak:

> "I can't derive meaningful acceptance criteria without understanding why this
> matters. Please articulate the business value, user impact, or risk this addresses
> — or acknowledge that you want to proceed without a clear justification."

If the user explicitly acknowledges proceeding without strong justification,
note this in the issue body under **Discussion**.

### Step 4: Capture Proposed Solution

Ask: **"How do you imagine solving this problem?"**

Capture the proposed approach, including important constraints, dependencies, or
sequencing decisions. If the user has no fixed approach yet, capture the constraints
that any solution must satisfy instead.

**Required output:** `Solution:` proposed approach (2-4 sentences), including constraints

### Step 5: Define Scope

Ask: **"What is explicitly in scope? What is explicitly out of scope?"**

Capture:

- **In scope:** Concrete deliverables, features, or changes
- **Out of scope:** Related work that is intentionally deferred

If the user is unsure about out-of-scope items, probe:

- "Are there related features you're deferring?"
- "What would make this item too large?"

**Required output:** In scope list + Out of scope list (may be empty if truly standalone)

---

## Phase 2: Derive Acceptance Criteria

**Goal:** Produce a complete, deduplicated, testable set of AC.

**Execution contract (applies to all Phase 2 steps):**

- Steps 1-9 are mandatory and must be executed in order
- After each step, produce the step output before continuing
- If a step gate fails, fix that step before advancing
- Before Phase 3, present a Step Completion Checklist for Steps 1-9

### Step 1: Ground the Purpose

Before deriving AC, state in one sentence what successful implementation looks like.
This anchors the derivation and prevents drift.

**Required output:**

- `Objective:` one sentence, concrete and user-impact oriented

**Gate:**

- Fail if objective is vague (e.g., "improve system") or not verifiable
- On failure: rewrite objective before Step 2

### Step 2: 5-Perspective Derivation

Derive candidate AC from each perspective. Not all perspectives apply to every item.

| Perspective      | Guiding Question                                                                      |
| ---------------- | ------------------------------------------------------------------------------------- |
| **Business**     | What user-visible outcomes must occur?                                                |
| **Architecture** | What system behaviors, integrations, or data flows must be correct?                   |
| **Technical**    | What code-level invariants must hold (error handling, logging, idempotency)?          |
| **Compliance**   | What audit, retention, or regulatory requirements apply?                              |
| **Security**     | What authentication, authorization, input validation, or data protection is required? |

For each perspective, generate WHEN/THEN criteria:

- **WHEN** [user action or system event] **THEN** [verifiable outcome]

Assign sequential IDs: `AC-BI-001`, `AC-BI-002`, etc.

**Required output:**

- `Perspective coverage` list: Business, Architecture, Technical, Compliance, Security
- `Candidate AC table` with AC-ID, Criterion, Perspective

**Gate:**

- Fail if no candidate AC are produced
- Fail if AC IDs are missing or non-sequential
- Fail if a perspective is marked applicable but has zero candidate AC
- On failure: regenerate Step 2 outputs before Step 3

### Step 3: Reflect Over Artifacts

Read the project's Domain Concepts and User Requirements (if they exist).

Ask:

- Do any domain entities have invariants that this work must preserve?
- Do any use cases have flows that this work touches?
- Are there related AC patterns from similar issues?

Add any missing AC discovered through this reflection.

**Required output:**

- `Artifact reflection notes:`
  - Domain invariant impacts
  - Use case flow impacts
  - Similar AC patterns reused
- `Delta AC:` added or updated AC IDs from reflection (or `none`)

**Gate:**

- Fail if artifacts exist but no reflection notes are provided
- On failure: complete reflection notes and AC delta before Step 4

### Step 4: Security Review

Review the entire AC set as a security engineer would during a design review.

**For each existing AC, ask:**

- Does this AC have implicit security assumptions that should be explicit?
- Does this action expose data that requires access control?
- Could a malicious actor abuse this flow?

**For the feature as a whole, check coverage of:**

| Security Concern       | Challenge Question                                                  |
| ---------------------- | ------------------------------------------------------------------- |
| Authentication         | Who can invoke this? Is identity verified before action?            |
| Authorization          | What permissions are required? Is there role/scope enforcement?     |
| Input Validation       | What inputs are accepted? Are boundaries and formats enforced?      |
| Data Protection        | Is sensitive data encrypted at rest/in transit? Masked in logs?     |
| Audit Trail            | Are security-relevant actions logged with actor, action, timestamp? |
| Rate Limiting          | Can this be abused through volume? Is throttling needed?            |
| Session/Token Handling | Are tokens scoped, time-limited, and properly invalidated?          |
| Error Handling         | Do errors leak implementation details or sensitive information?     |

**Output:**

- Add new AC for uncovered security concerns (assign next `AC-BI-###` IDs)
- Modify existing AC to add security specificity where needed

**Required output:**

- `Security coverage matrix` for the 8 concerns (covered by AC-ID or explicit N/A with reason)
- `Security delta AC:` new/updated AC IDs from security review

**Gate:**

- Fail if any security concern is neither mapped to AC nor justified as N/A
- On failure: add/modify AC and repeat security review before Step 5

### Step 5: Deduplicate

Review all candidate AC (including those added or modified in Step 4). Merge or eliminate:

- AC that test the same behavior with different wording
- AC that are subsumed by a broader AC
- AC that duplicate existing system behavior (already covered elsewhere, including in
  other tracked issues — check dependency/related issues before assuming a gap)

**Required output:**

- `Dedup log:` AC IDs merged/removed and rationale
- `Post-dedup AC set`

**Gate:**

- Fail if duplicate AC remain without rationale
- On failure: complete dedup log and refresh AC set before Step 6

### Step 6: Eliminate Low Value

Remove AC that:

- Cannot influence implementation decisions (obvious/trivial)
- Cannot fail (tautological)
- Test the framework, not the feature

For borderline cases, ask: **"What implementation mistake would this AC catch?"**
If no answer → eliminate.

**Required output:**

- `Low-value elimination log:` removed AC IDs + mistake-caught rationale for retained borderline AC
- `Post-elimination AC set`

**Gate:**

- Fail if retained borderline AC have no mistake-caught rationale
- On failure: eliminate or justify before Step 7

### Step 7: Testability Gate

Every remaining AC must pass:

| Check                    | Pass                             | Fail                         |
| ------------------------ | -------------------------------- | ---------------------------- |
| Has action/event trigger | "WHEN user submits form"         | "Form works"                 |
| Has verifiable outcome   | "THEN tenant appears in list"    | "System behaves correctly"   |
| Is specific              | "THEN redirect to /tenants/{id}" | "THEN user sees result"      |
| Can be verified          | Clear assertion possible         | Requires subjective judgment |

**Gate:** Do not proceed until all AC pass. Rewrite or eliminate failures.

**Required output:**

- `Testability check table:` each AC-ID mapped to pass/fail on all 4 checks
- `Remediation actions:` rewritten/eliminated AC IDs

**Gate:**

- Fail if any AC has a failed check
- On failure: remediate and rerun Step 7 before Step 8

### Step 8: Completeness Check

Validate the AC set against the original objective stated in Step 1, and against the
Deliverables captured in Phase 1 Step 5.

**Ask:** "Are these AC complete with respect to the objective and every deliverable?"

Review each aspect of the stated objective and verify at least one AC tests it:

| Objective Aspect | Covered By AC | Gap? |
| ---------------- | ------------- | ---- |
| [aspect 1]       | AC-BI-###     | —    |
| [aspect 2]       | —             | Yes  |

**If gaps exist:**

1. Derive AC for uncovered aspects
2. Apply testability check to new AC
3. Add passing AC to the set

Proceed only when every aspect of the objective has corresponding AC. Deliverables that
are decisions-to-record rather than runtime behavior (e.g. "decision recorded on X") do
not need a matching AC — note this explicitly rather than forcing an artificial one.

**Required output:**

- `Objective coverage table` (objective aspect -> AC-ID)
- `Gap closure log:` added AC IDs (or `none`)

**Gate:**

- Fail if any objective aspect has no covering AC
- On failure: add missing AC and rerun Step 7 then Step 8

### Step 9: Group for Implementation

Group AC in logical implementation sequence and renumber IDs accordingly.

**This step is mandatory and non-skippable.**

**Grouping principles:**

| Priority | Category                | Rationale                                      |
| -------- | ----------------------- | ---------------------------------------------- |
| 1        | Authentication/Identity | Must establish "who" before authorizing "what" |
| 2        | Authorization/Access    | Gates all protected operations                 |
| 3        | Data model/Schema       | Foundation for business logic                  |
| 4        | Core happy path         | Primary user value                             |
| 5        | Validation/Constraints  | Guard rails for core flow                      |
| 6        | Error handling          | Graceful degradation                           |
| 7        | Audit/Logging           | Observability layer                            |
| 8        | Edge cases              | Completeness                                   |

Category names may be adapted to the domain (e.g. "Transport", "Deployment
independence") as long as priority order is preserved and the rationale is clear.

**Process:**

1. Assign each AC to a category based on what it tests
2. Order AC within each category by dependency (if A must exist for B to work, A comes first)
3. Renumber all AC sequentially: `AC-BI-001`, `AC-BI-002`, etc. in the new order
4. Produce output as **one section per group** using the template below

**Required output format (do not collapse into one flat table):**

```markdown
### Group 1: Authentication/Identity

| AC-ID     | Criterion                    | Perspective |
| --------- | ---------------------------- | ----------- |
| AC-BI-001 | WHEN [action] THEN [outcome] | Security    |

### Group 2: Authorization/Access

| AC-ID     | Criterion                    | Perspective |
| --------- | ---------------------------- | ----------- |
| AC-BI-002 | WHEN [action] THEN [outcome] | Security    |
```

If a group has no AC, omit that group.

**Validation gate:**

- Fail if output contains a single global AC table
- Fail if group headers are missing
- Fail if AC IDs are not sequential across groups
- On failure: regenerate Step 9 output before proceeding

**Required output:**

- Grouped AC sections in implementation order
- `Renumber map:` old AC-ID -> new AC-ID (or `identity`)

**Gate:**

- Fail if grouped sections are missing or out of priority order without rationale
- On failure: regenerate Step 9 output before Phase 3

### Step 10: Phase 2 Completion Checklist

Before entering Phase 3, present and pass this checklist:

| Step                                | Evidence Present | Pass/Fail |
| ----------------------------------- | ---------------- | --------- |
| 1 Objective grounded                | Yes/No           | Pass/Fail |
| 2 Candidate AC derived              | Yes/No           | Pass/Fail |
| 3 Artifact reflection done          | Yes/No           | Pass/Fail |
| 4 Security review done              | Yes/No           | Pass/Fail |
| 5 Dedup complete                    | Yes/No           | Pass/Fail |
| 6 Low-value elimination complete    | Yes/No           | Pass/Fail |
| 7 Testability gate passed           | Yes/No           | Pass/Fail |
| 8 Completeness gate passed          | Yes/No           | Pass/Fail |
| 9 Grouping and renumbering complete | Yes/No           | Pass/Fail |

**Gate:**

- Do not proceed to Phase 3 unless all rows are `Pass`
- If any row fails, return to the corresponding step

The final AC table reflects implementation order — `AC-BI-001` is the first thing to build.

---

## Phase 3: Assess Scope

### Step 1: Size

Score the issue:

| Factor                  | S (1) | M (2) | L (3) | XL (4) |
| ----------------------- | ----- | ----- | ----- | ------ |
| Containers affected     | 1     | 2     | 3     | 4+     |
| Use cases touched       | 1     | 2-3   | 4-5   | 6+     |
| Artifact layers touched | 1-2   | 2-3   | 3-4   | All 4  |
| New domain concepts     | 0     | 1-2   | 3-4   | 5+     |

Take the **highest single factor** as the T-shirt size.

- **S/M:** Proceed
- **L:** Warn — "This affects [N] containers/layers. Consider splitting. Proceed anyway?"
- **XL:** Recommend split — present decomposition proposal before proceeding

**Split strategies** (use the most applicable):

- **By container** — one item per affected container
- **By artifact layer** — separate "define requirements" from "implement API" from "build UI"
- **By use case** — one item per use case or tightly related group
- **By concern** — separate functional changes from NFR changes

### Step 2: Classify

Determine the change type (used for branch prefix):

| Type               | Branch Prefix  |
| ------------------ | -------------- |
| New Feature        | `feature/`     |
| Bug Fix            | `bugfix/`      |
| Design             | `feature/`     |
| Regulatory         | `feature/`     |
| System Improvement | `improvement/` |

---

## Phase 4: Confirm

Present the complete issue, following `.github/ISSUE_TEMPLATE/standard-mom.md`:

**Title:** [concise, 5-10 words]

## Problem

[From Phase 1 Step 1 — the concrete problem, challenge, or risk. Fold in the
validated "why" from Step 2 where it clarifies stakes.]

## Solution

[From Phase 1 Step 4 — the proposed approach and its key constraints.]

## Value

[From Phase 1 Step 2 — why the outcome matters now, for sponsors/maintainers/users.]

## Deliverables

- [ ] [Item from Phase 1 Step 5's in-scope list]
- [ ] [Item from Phase 1 Step 5's in-scope list]

## Discussion

[Optional. Out-of-scope items from Phase 1 Step 5, open questions, references,
or a note that the user proceeded without a strong justification (Phase 1 Step 3).]

## Acceptance criteria

### Group 1: [category]

| AC-ID     | Criterion                    | Perspective |
| --------- | ---------------------------- | ----------- |
| AC-BI-001 | WHEN [action] THEN [outcome] | Business    |

### Group 2: [category]

| AC-ID     | Criterion                    | Perspective |
| --------- | ---------------------------- | ----------- |
| AC-BI-002 | WHEN [action] THEN [outcome] | Security    |

## Size

[S/M/L] — [brief rationale]

## Related

- [ADR-nnn: title]
- Depends on: [#issue]
- Enables: [future work]

**Gate:** Do not create until user explicitly confirms.

---

## Phase 5: Create

1. Serialize the confirmed content from Phase 4 as GitHub-flavored Markdown, preserving
   section headers, group headers, and `AC-BI-###` IDs exactly as confirmed.
2. Create the issue:

   ```bash
   gh issue create \
     --repo <owner/repo> \
     --title "<title>" \
     --body-file <temp-file> \
     --assignee @me
   ```

3. If `github.project` is configured in `system-config.md`, add the issue to that
   project and set Status = Backlog.

Execution should be silent unless:

- Creation fails
- `github.project` is configured but the project cannot be found

---

## Rules

1. **Why before what** — refuse to derive AC for unjustified work
2. **Testable or eliminate** — no vague AC survive Phase 2
3. **Artifact-grounded** — AC derivation must consult Domain Concepts and Use Cases
4. **One question at a time** — show remaining count
5. **`AC-BI-###` IDs persist** — through follow-on issues and implementation; check
   related/dependency issues in Step 5 (Dedup) before minting an AC that already exists
   elsewhere
6. **Step 9 grouping is mandatory** — output must be sectioned by group with one table per group
7. **Phase 2 steps are non-skippable** — provide required output and pass gate for every step
8. **Standard-mom headers only** — `Problem`, `Solution`, `Value`, `Deliverables`,
   `Discussion` (optional), `Acceptance criteria`, `Size`, `Related`. Do not introduce
   `Background`/`Scope` or any tracker-specific structure.
