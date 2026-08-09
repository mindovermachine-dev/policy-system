<!-- © 2026 Cartman ApS. All rights reserved. -->
# Backlog: FINDING-001 — Penalty/Enforcement Ingestion Gap

**Status:** Open. Does not block the `skill-transfer` spike verdict (all
FINDING-001 cases resolved as correct refusals in both the dev and held-out
runs) — this is real ingestion/product work, scoped out of the dataset
maintenance pass ([NEXT-ACTIONS.md](./NEXT-ACTIONS.md) thread 3) because it
changes what the graph contains, not what an existing golden answer says.

No work-tracking system is configured for this project (no
`system-config.md`), so this is recorded here rather than filed as a tracked
work item. Convert to a tracked backlog item once one exists.

## The gap

`graph-ingestion3` extracted obligations and Annex I / Art. 13–14-style
requirements, but not:

- **Penalty/enforcement chapters**: GDPR Art. 83(5) fines, NIS2 Art. 23(3)
  significance thresholds, NIS2 Art. 34 / CRA Art. 64 penalty tiers.
- **Final provisions**: CRA Art. 71 phasing dates.
- **Definition paragraphs within otherwise-extracted articles**: CRA
  Art. 14(5) — the operative paragraphs (14(3)–(4)) were extracted, the
  definition paragraph was not. Extraction should cover whole articles, not
  just operative clauses.

## Why it matters

Confirmed across 9 questions total (4 dev-set: LC-E2, RM-E2, EM-M3, SEC-H3;
5 held-out: LC-E3, LC-M3, LC-M4, RM-E3, RM-E4) — all correctly refused by
the skill, but "what's our fine exposure" and "when do these phased
obligations kick in" are questions real Legal Counsel / Compliance users
will ask in production. A correct refusal in a spike is a real gap in a
product.

## Suggested fix

Extend `graph-ingestion3` to extract penalty/enforcement articles and final
provisions into `Requirement` nodes, and fix the partial-article extraction
so a Requirement's definition sub-clauses are captured alongside its
operative ones — same pattern as the rest of the pipeline, just wider
article coverage.

## Source

[RUNBOOK.md](./RUNBOOK.md) — dev-set FINDING-001 (single source), held-out
FINDING-001 extension (two new variants: final provisions, partial-article
definitions).
