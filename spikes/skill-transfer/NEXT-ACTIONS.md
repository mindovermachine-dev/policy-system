<!-- © 2026 Cartman ApS. All rights reserved. -->
# Next Actions: Skill-Transfer Spike Follow-On

**Date:** 2026-08-09
**Source:** Reflections on the dev-v1 and blind-v1 runs (see
[RUNBOOK.md](./RUNBOOK.md) for full results). Proposed sequencing:
**4 → 3 → 2 → 1 → 5** — the methodology question (4) determines what dataset
work (3) is worth doing, which determines what skill v2 (2) can actually be
validated against.

**Status (2026-08-09): all five threads resolved and executed** in
sequence 4 → 3 → 2 → 1 → 5. Summary:

| # | Decision | Artifact |
|---|---|---|
| 4 | Generate blind-set v2 in an isolated workspace, folding fixed goldens into a new frozen catalog | [BLIND-SET-V2-KICKOFF.md](./BLIND-SET-V2-KICKOFF.md) (not yet run — needs a fresh isolated session) |
| 3 | Scoped to goldens-only; FINDING-001 filed separately, non-blocking | [GOLDEN-FIXES.md](./GOLDEN-FIXES.md) (applied to `ps-questions/blind-answers.md`), [BACKLOG-FINDING-001.md](./BACKLOG-FINDING-001.md) |
| 2 | Three definition packs + FINDING-002 fix written into the skill | [`ps-domain/SKILL.md`](../../.github/skills/ps-domain/SKILL.md) |
| 1 | AD-6 narrowed to "holds for shape, not semantic definitions," dated revision note added | [`ps-prototype-architecture.md`](../../docs/architecture/ps-prototype-architecture.md) |
| 5 | Hard-tier grading requires independent review going forward | [HELD-OUT-KICKOFF.md](./HELD-OUT-KICKOFF.md) (step 5 updated) |

Remaining before `cli-tool-semantics` can start: **run blind-set v2
generation** (a fresh, isolated session — this repo's context is now
contaminated with these findings and cannot generate it), then the
confirmatory evaluation of skill v2 against it with independent Hard-tier
grading.

---

## 1. Close the loop on the spike's own verdict: revise AD-6

The spike verdict is "AD-6 needs revision," but
[ps-prototype-architecture.md](../../docs/architecture/ps-prototype-architecture.md)
hasn't been touched. The revision is narrow and well-evidenced: AD-6 holds
for grounding *shape* (schema, IDs, directions, provenance) but not for
*semantic definitions*. The architecture decision should say explicitly that
a skill must carry canonical predicate definitions ("overdue," "stale,"
"blast radius"), not just schema knowledge. Until AD-6 is updated, the
learning is recorded in a runbook but not in the artifact that future work
will consult.

## 2. Skill v2 — the three definition packs

The blind-set failure classes map directly to three concrete skill additions:

- **Boundary rules as named definitions** — pin "overdue excludes
  deprecated," "stale = broken chain, not lapsed review." Cheap to write;
  addresses the largest failure class (4/10).
- **Narrowing discipline** — extend the provenance rule with its inverse:
  cite the chain that *routes through* the named node, not its siblings.
  Addresses blast-radius over-claiming (2/10).
- **Unit-of-counting discipline** — "state what you're counting before you
  count it" (chains vs controls vs obligations). Addresses granularity slips
  (2/10).

Plus **FINDING-002**: document the `STARTS WITH` article-boundary over-match
explicitly (match base article, then filter on the character after the prefix
being a letter or end-of-string).

Worth noting: do **not** re-run the blind set after skill v2 under the
current discipline — it is frozen, single-run, and already consumed. The
re-validation question (action 4) needs answering first.

## 3. Dataset maintenance before any re-evaluation

- **FINDING-003**: fix the four defective blind goldens (RM-M2 scope,
  SA-M4 under-count, SEC-H4 outdated premise, AU-M4/EM-E3 inconsistent
  staleness). Evaluating skill v2 against known-defective goldens would
  poison the measurement.
- **FINDING-001**: ingestion gap — penalty/enforcement chapters (GDPR
  Art. 83, NIS2 Art. 23/34, CRA Art. 64), final provisions (CRA Art. 71),
  and *whole articles* (the Art. 14(5) partial-extraction variant). This is
  product value, not just test hygiene: "fine exposure" is a question real
  Legal Counsel users will ask.

## 4. Resolve the methodological tension: blind-set reusability

The current protocol says the blind set is fetched once, run once, no
iteration afterward — the unbiased estimate. But FINDING-003 shows 4/54
goldens are defective, and a skill v2 run would want a *new* unbiased
estimate. Options:

- **Generate blind-set v2** in a structurally isolated clean workspace (per
  the isolation rule in [README.md](./README.md)), reusing the fixed goldens
  as part of a new frozen catalog.
- **Split future blind sets into thirds** (dev / validation / final) so
  iteration doesn't consume the final set.
- **Accept the current blind set as consumed** and treat the next evaluation
  as a fresh experiment.

This decision gates action 2's value — skill v2 without a re-validation path
is an unverified hypothesis.

## 5. Minor: rubric grading provenance

Dev-set Hard-tier grading was done by the session agent (grading option A) —
self-grading, essentially. Before the next run, decide whether Hard rubric
grades need independent review, or accept the bias risk explicitly.
