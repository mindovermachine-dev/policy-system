<!-- © 2026 Cartman ApS. All rights reserved. -->
# Golden Fixes: FINDING-003

**Status:** Applied 2026-08-09 to `ps-questions/blind-answers.md` (added to
the workspace mid-session). The goldens live in that separate repo,
deliberately kept out of this one (see the isolation rule in
[README.md](./README.md)) — changes are in `ps-questions`'s working tree,
uncommitted pending review.

**Apply as part of:** blind-set v2 generation (thread 4 of
[NEXT-ACTIONS.md](./NEXT-ACTIONS.md)) — fold these fixes into the new frozen
catalog rather than patching the current (consumed) blind set in place.

All four were verified against the live graph by read-only query during
held-out grading; see [RUNBOOK.md](./RUNBOOK.md) for the original evidence.

---

## RM-M2

- **Question:** CRA-required-and-ungoverned capability count.
- **Current golden:** 55 (the unfiltered capability set).
- **Fix:** 22 ungoverned, out of 29 CRA-required capabilities. The question
  is explicitly CRA-scoped; the golden must filter to that scope before
  counting.

## SA-M4

- **Question:** which Art. 32 sub-clause capabilities lack an approved
  policy.
- **Current golden:** 3-of-6 (under-count).
- **Fix:** 4-of-6. Missing capability:
  `cap_availability_resilience_7caf2b` (Art. 32.1(b)) — verified to have no
  approved Policy in the graph.

## SEC-H4

- **Question:** blast radius if a named control fails, including whether the
  CRA/encryption link is enumerated in the dataset.
- **Current golden:** asserts "dataset does not enumerate the CRA→encryption
  link."
- **Fix:** that premise is outdated — the `REQUIRES` edge exists in the
  graph. Rewrite the golden to reflect the edge's presence, and re-derive the
  expected blast-radius scoping accordingly (apply the thread-2 narrowing
  discipline: scope to chains that actually route through the failing
  control, not siblings).

## AU-M4 / EM-E3

- **Question pair:** staleness/evidence-chain questions that depend on
  whether an overdue-review chain counts as "stale."
- **Verified against the live graph (2026-08-09):** both goldens' *article/
  chain selections* already match the canonical definition — **stale =
  broken chain** (Policy not approved, or Standard not implemented/reviewed,
  or Control not implemented), **not** a live chain with a lapsed review
  (that's "overdue"). AU-M4's `{32.4, 37, 38}` set is unchanged; Art. 33 and
  32(1)(c) are correctly excluded because each has an overdue-but-intact
  chain alongside a broken one.
- **Fix applied:** added the explicit definition inline to both entries so
  future graders/agents can't read them either way — this was the actual
  gap (an implicit rule two different graders could apply inconsistently),
  not a wrong number.
- **New open item, not resolved here:** re-verifying EM-E3 independently
  turned up a total-chain-count discrepancy (38/21 by direct traversal vs.
  the golden's 57/31) — left as-is rather than guessed at; see the note
  inline in `blind-answers.md` and re-derive the original computation
  method before this golden is used in blind-set v2.

---

## Not in scope here

FINDING-001 (penalty/enforcement ingestion gap) is tracked separately in
[BACKLOG-FINDING-001.md](./BACKLOG-FINDING-001.md) — it changes what the
graph contains, not what an existing golden should say, and was scoped out
of this dataset-maintenance pass per the goldens-only decision.
