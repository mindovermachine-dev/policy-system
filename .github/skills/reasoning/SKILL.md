---
name: reasoning
description: >-
  Defines the reasoning flows, compliance triggers, and principles that govern how agents reason.
metadata:
  author: Tete Mensa-Annan
  version: "1.0.0"
  tags: [thinking, reason, help]
  copyright: "© 2026 Cartman ApS. All rights reserved."
---

# Reasoning Discipline

This instruction defines cross-cutting reasoning principles and compliance triggers
that ALL Synesis agents must apply. It is not a skill (no phases, no inputs/outputs) —
it is behavioral infrastructure that governs how agents think and act.

**Version:** 1.0.0

---

## Principles

Before responding to any non-trivial request, apply these principles:

1. **Verify claims against artifacts (DP-001)** — when stating how something works,
   confirm by reading the actual file. Do not assume templates, skills, or configs
   match your mental model.
2. **Assign IDs to traceable outputs (DP-002)** — when producing lists, criteria,
   options, or recommendations that may be referenced later, assign stable IDs
   (e.g., AC-001, DP-001, Option A).
3. **Acknowledge multiple valid approaches (DP-003)** — when a question has more
   than one valid answer, state this explicitly. For complex decisions, use the
   3P Algorithm.
4. **Include counter-cost in trade-off analysis (DP-004)** — when evaluating whether
   to do or not do something, calculate both the cost of doing AND the cost of
   not doing. Never compare overhead against zero.
5. **Check standards alignment (DP-005)** — when evaluating approaches, identify
   applicable industry standards and check alignment. Misalignment is evidence of
   a known defect class.

---

## Quick Reasoning

For simple, straightforward questions:

1. **UNDERSTAND:** What is the core question being asked?
2. **ANALYZE:** What are the key factors/components involved?
3. **REASON:** What logical connections can I make? Apply Principles above.
4. **SYNTHESIZE:** How do these elements combine?
5. **CONCLUDE:** What is the most accurate/helpful response?

---

## 3P Algorithm

For proposals, complex problems, or when multiple valid approaches exist:

0. **Research (optional):** Ask the user if you should search the web for best practices
   and relevant information before making proposals. If yes, perform web research and
   summarize findings first.
1. **Generate 3 Proposals** — three distinct approaches (not minor variations)
2. **Critique Each** — apply Principles:
   - Strengths, flaws, trade-offs
   - Counter-cost analysis (DP-004): cost of each approach AND cost of not choosing it
   - Standards alignment (DP-005): check against applicable industry standards
3. **Synthesize** — combine best elements, address flaws, balance trade-offs
4. **Final Critique** — remaining flaws, blind spots, new issues from synthesis
5. **Final Proposal** — clear rationale, known limitations, implementation considerations

---

## Compliance Triggers

These are forced pause points — when a trigger fires, STOP and verify BEFORE
proceeding. Do not rely on memory; verify against the actual artifacts.

| Trigger | Verification Required | DP/WF |
|---------|----------------------|-------|
| User approves a proposal ("yes", "proceed", "do it") | Verify 3P algorithm was applied if the proposal involved multiple options or complex trade-offs. If 3P was skipped, apply it now before acting. | DP-003 |
| Generating a list of items that may be referenced later | Verify IDs are assigned (AC-001, DP-001, Option A, etc.) | DP-002 |
| Stating how an artifact, skill, or template works | Verify by reading the actual file — do not rely on mental model | DP-001 |
| Proposing to add/skip/modify a process step | Calculate counter-cost: cost of doing AND cost of not doing | DP-004 |
| Modifying a template or skill instruction | Verify the target template has a structural slot for any mapped data | DP-001 |
| Evaluating competing approaches | Identify applicable standards and check alignment | DP-005 |
| Starting skill execution | Validate prerequisites from prior workflow phases exist and are complete | WF-009 |
| Test failures during implementation | STOP. Run full test suite. Categorize all failures by root cause before fixing any single failure. Fix root causes in order of impact (highest first). Do not reactively fix one error at a time. | DP-003 |

**If a trigger fires and verification fails:**
- STOP and inform the user which verification failed
- Offer to remediate before proceeding
- Do NOT proceed without either passing verification or explicit user override
