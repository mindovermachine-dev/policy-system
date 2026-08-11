---
metadata:
  author: Cartman ApS
  copyright: "© 2026 Cartman ApS. All rights reserved."
  version: "0.1.0"
---

# Business Challenge Rubric

Use this rubric for both bootstrap and refine workflows for Business Challenge.

---

## Field Scoring

Each field is scored independently. The overall Business Challenge passes only if all mandatory fields pass their individual gates.

---

### 1. Title

**Complexity**: Simple  
**Purpose**: Clear, concise identifier that signals the challenge domain.

| ID | Criterion | Description | Pass (2) | Partial (1) | Fail (0) |
|----|-----------|-------------|----------|-------------|----------|
| BC-T-001 | Clarity | Conveys challenge domain concisely | Immediately conveys the challenge domain in ≤10 words | Understandable but verbose or slightly ambiguous | Vague, jargon-heavy, or misleading |
| BC-T-002 | Distinctiveness | Distinguishable from other challenges | Distinguishable from other Business Challenges in the cascade | Similar to another but context-dependent | Duplicate or generic placeholder |

**Pass Gate**: BC-T-001 Pass AND BC-T-002 Pass

---

### 2. Description

**Complexity**: Medium  
**Purpose**: Articulates the tension or gap that makes this challenge necessary.

| ID | Criterion | Description | Pass (2) | Partial (1) | Fail (0) |
|----|-----------|-------------|----------|-------------|----------|
| BC-D-001 | Tension Articulation | States current vs. desired state | Clearly states current state vs. desired state | Implies tension but one side is vague | No tension evident |
| BC-D-002 | Strategic Context | Links to parent Strategic Intent | Links to parent Strategic Intent or cascade context | Reference exists but weak connection | No link to cascade |
| BC-D-003 | Stakeholder Impact | Names who is affected and how | Names who is affected and how | Impact implied but stakeholders unnamed | No impact described |

**Pass Gate**: BC-D-001 Pass AND (BC-D-002 Pass or Partial)

---

### 3. Objective

**Complexity**: Complex  
**Purpose**: Defines the measurable business outcome to achieve.

| ID | Criterion | Description | Pass (2) | Partial (1) | Fail (0) |
|----|-----------|-------------|----------|-------------|----------|
| BC-O-001 | Outcome-First | Describes outcome, not activity | Describes business outcome, not project activity | Outcome implied but activity-leaning | Task-focused (e.g., "implement X") |
| BC-O-002 | Measurability | Has baseline, target, and timeframe | Baseline → target with magnitude and timeframe | Direction clear but baseline TBD | Unmeasurable or vague |
| BC-O-003 | Strategic Relevance | Traceable to parent Strategic Intent | Explicit traceable link to parent Strategic Intent | Link exists but not explicit | No traceable link |
| BC-O-004 | Stretch with Realism | Ambitious yet plausible | Ambitious yet plausible given constraints | Leans toward sandbagging or stretch | Fantasy target or status quo |
| BC-O-005 | Decision-Useful | Reviewers would score identically | Two reviewers would score progress identically | Minor ambiguity but scorable | Success subjective or undefined |
| BC-O-006 | Learning-Fit | States learning needs if uncertain | If uncertain, states what must be learned and by when | Learning intent implied | High uncertainty with hard target |
| BC-O-007 | Inspiration Quality | Inspires action beyond status quo | Language motivates meaningful change and effort | Functional but uninspiring | Demoralizing or business-as-usual |
| BC-O-008 | Beneficiary Clarity | Specifies who benefits | Explicitly names the population or stakeholder who benefits | Beneficiary implied but not stated | No beneficiary identifiable |
| BC-O-009 | Urgency | Explains why this is important now | Clearly articulates the urgency or timing | Urgency implied but not explicit | No urgency stated |

**Pass Gate**: BC-O-001 Pass AND BC-O-002 Pass AND (BC-O-005 Pass or Partial) AND total score ≥ 10/16

---

### 4. Anti-goals

**Complexity**: Medium  
**Purpose**: Explicit boundaries to prevent gaming, local optimization, unintended harm, and misaligned trade-offs.

| ID | Criterion | Description | Pass (2) | Partial (1) | Fail (0) |
|----|-----------|-------------|----------|-------------|----------|
| BC-AG-001 | Boundary Clarity | States what will NOT be pursued | Clear boundaries stated | Boundaries implied but not explicit | No boundaries stated |
| BC-AG-002 | Risk Mitigation | Addresses gaming or harm scenarios | Foreseeable gaming or harm scenarios addressed | Some risks addressed, gaps remain | High-stakes unguarded |
| BC-AG-003 | Trade-off Focused | Addresses sacrifice temptations | Names what not to give up for the goal | Some trade-offs mentioned | No trade-off guidance |
| BC-AG-004 | Observability | Anti-goals can be monitored | Observable and monitorable | Observable but monitoring unclear | Unverifiable |

**Pass Gate**: BC-AG-001 Pass AND (BC-AG-002 Pass or Partial)

---

### 5. Main Effort

**Complexity**: Medium  
**Purpose**: The primary initiative or workstream that will deliver the objective.

| ID | Criterion | Description | Pass (2) | Partial (1) | Fail (0) |
|----|-----------|-------------|----------|-------------|----------|
| BC-M-001 | Singularity | One clearly identified main effort | One clearly identified main effort | Primary effort identifiable among several | Multiple competing efforts, none primary |
| BC-M-002 | Outcome Linkage | Connects to objective achievement | Logical connection to objective achievement | Connection plausible but not explicit | No clear link to objective |
| BC-M-003 | Actionability | Decomposable into work packages | Clear enough to decompose into work packages | High-level but decomposable | Abstract or undefined |
| BC-M-004 | Ownership Clarity | Accountable party identifiable | Accountable party identifiable or assignable | Role type clear, individual TBD | No ownership model |

**Pass Gate**: BC-M-001 Pass AND (BC-M-002 Pass or Partial)

---

### 6. Key Results

**Complexity**: Complex  
**Purpose**: Measurable milestones that evidence progress toward the objective.

| ID | Criterion | Description | Pass (2) | Partial (1) | Fail (0) |
|----|-----------|-------------|----------|-------------|----------|
| BC-K-001 | Measurability | Has metric, baseline, target, deadline | Each KR has metric, baseline, target, deadline | Most elements present, minor gaps | Vague or unmeasurable |
| BC-K-002 | Leading Indicators | Includes leading (not just lagging) indicators | At least one KR is a leading (not lagging) indicator | Mix of leading/lagging but lagging-heavy | All lagging indicators |
| BC-K-003 | Collective Sufficiency | KRs prove objective achievement | KRs together would prove objective achievement | Coverage gaps but majority covered | KRs unrelated to objective |
| BC-K-004 | Independence | KRs not double-counting outcomes | KRs are not double-counting same outcome | Minor overlap acknowledged | Redundant or circular KRs |
| BC-K-005 | Stretch Balance | KRs ambitious but achievable | KRs are ambitious but achievable | Uneven stretch across KRs | Sandbagging or fantasy |

**Pass Gate**: (BC-K-001 Pass or Partial for all KRs) AND BC-K-003 Pass

---

### 7. Constraints

**Complexity**: Simple  
**Purpose**: Boundaries that scope the solution space.

| ID | Criterion | Description | Pass (2) | Partial (1) | Fail (0) |
|----|-----------|-------------|----------|-------------|----------|
| BC-C-001 | Explicitness | Constraints are stated, not assumed | Constraints are stated, not assumed | Some constraints explicit, others implied | Constraints absent or hidden |
| BC-C-002 | Realism | Achievable within constraints | Constraints are achievable within them | Tight but possible | Contradictory or impossible |
| BC-C-003 | Non-Contradiction | Constraints do not conflict | Constraints do not conflict with each other | Minor tension but resolvable | Direct contradiction |

**Pass Gate**: (BC-C-001 Pass or Partial) AND BC-C-003 Pass

---

### 8. Assignments

**Complexity**: Simple  
**Purpose**: Who is responsible for what aspects of delivering the challenge.

| ID | Criterion | Description | Pass (2) | Partial (1) | Fail (0) |
|----|-----------|-------------|----------|-------------|----------|
| BC-A-001 | Named Accountability | Specific ownership identified | Person or role named per area | Partial accountability | "The team" or unassigned |
| BC-A-002 | Scope Clarity | Accountabilities bounded and distinct | Non-overlapping scopes | Some overlap or ambiguity | Overlapping or undefined |
| BC-A-003 | Authority Match | Decision rights match accountability | Accountable parties can decide | Some mismatches | Accountable but powerless |

**Pass Gate**: BC-A-001 Pass AND BC-A-003 Pass

---

## Overall Business Challenge Pass Gate

Business Challenge passes if:

1. **Title** passes
2. **Description** passes
3. **Objective** passes
4. **Anti-goals** passes
5. **Main Effort** passes
6. **Key Results** passes
7. **Constraints** passes
8. **Assignments** passes

---
