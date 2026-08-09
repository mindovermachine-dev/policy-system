<!-- © 2026 Cartman ApS. All rights reserved. -->
# Kickoff Prompt: Blind-Set v2 Generation

**Superseded 2026-08-09** (see [NEXT-ACTIONS.md](./NEXT-ACTIONS.md), thread
4): decided that reusing the existing blind set is sufficient — a spike not
having seen it during its own development is enough isolation, without
regenerating a v2 in a structurally isolated workspace. Kept below as a
record of the option considered and rejected, not as a live task.

**Do not run this in the current session.** This session has read
`RUNBOOK.md`, `NEXT-ACTIONS.md`, and specific held-out question IDs and
their failure patterns — exactly the contamination the isolation rule in
[README.md](./README.md) exists to prevent. Blind-set v2 must be generated
by a **fresh session in a clean workspace** containing only the allowed
inputs below. Paste the block as the first message of that session.

## Why v1 can't just be relabeled

Skill v2 ([`ps-domain/SKILL.md`](../../.github/skills/ps-domain/SKILL.md))
was written by directly analyzing v1's held-out failures — its three
definition packs exist *because of* AU-M4, EM-E3, SA-M4, SEC-H4, and the
others. Running skill v2 against the same v1 questions (even with
[FINDING-003](./GOLDEN-FIXES.md)'s 4 goldens corrected) is not an unbiased
generalization check — the skill was tuned to those exact failure classes.
Blind-set v2 must be a **new, independently generated** question set.

---

```
You are generating the held-out (blind) validation set, v2, for the
Policy System skill-transfer spike. Work autonomously, but ask before any
state-changing action beyond writing files.

## Isolation constraints — non-negotiable

- This workspace must contain ONLY: ps-domain-concepts.md (the domain
  model), the FalkorDB graph schema, and sample data from the policy_system
  graph. It must NOT contain `c4b-ps-internal/spikes/` (any spike folder),
  RUNBOOK.md, NEXT-ACTIONS.md, GOLDEN-FIXES.md, example-questions.md, or any
  other artifact that documents prior question sets, failure modes, or
  findings. If any of those are reachable from this workspace, stop and
  flag it — do not proceed.
- Generate questions from the domain model and graph shape only. Do not
  aim for particular failure classes, boundary cases, or "gotchas" you may
  already know about from training — the generation must be blind by
  construction, not just by directory structure.

## Task

1. Generate a blind question catalog structurally matching the existing
   dev/held-out format: audience-tiered (Legal Counsel, Compliance Officer,
   Solutions Architect, Auditor, Risk Manager, Product Manager, Software
   Engineer, Security Engineer, Engineering Manager), difficulty-tiered
   (Easy/Medium/Hard), natural/canonical register mix, ~50-60 questions.
2. For each question, compute the golden answer/grading criteria by
   querying the live graph directly (read-only) — never by inference or
   memory. Record the verification query alongside each golden, the same
   way the existing `blind-answers.md` appendix does.
3. Pay explicit attention when computing "boundary" answers (overdue vs.
   stale, counting units, blast-radius scope) — get these right the first
   time by writing out the counting/scoping rule you're applying before you
   query, and re-verify with a second differently-shaped query before
   finalizing any answer.
4. Review the generated set for diversity before finalizing — check it
   isn't only graph-shaped questions, and covers a reasonable spread of
   query shapes (lookup, aggregate, cross-layer, discovery/refusal).
5. Output: `blind-questions-v2.md` and `blind-answers-v2.md` in this
   workspace, matching the structure of the existing v1 files.

## Deliverable

The new frozen catalog, plus a short generation note: how many questions,
tier/audience distribution, and confirmation that no spike-folder content
was reachable from this workspace during generation.
```

---

## Notes for the operator

- Once generated, `blind-questions-v2.md` / `blind-answers-v2.md` become the
  new frozen set — move them into `ps-questions` (replacing or
  supplementing v1's files; decide at that point whether v1's still-valid
  50 questions are worth keeping alongside v2, or whether v2 fully
  replaces v1).
- **Independent grading** (thread 5 decision): when this set is later run
  against skill v2, Hard-tier grading must be done by a reviewer (session or
  human) that did not perform the run — mirror the isolation discipline
  used here. Update [HELD-OUT-KICKOFF.md](./HELD-OUT-KICKOFF.md)'s
  evaluation prompt to add this before the next confirmatory run.
- One run only, same discipline as v1: once this set has been used for a
  confirmatory evaluation, it is consumed. Any further skill iteration needs
  a v3, generated the same isolated way.
