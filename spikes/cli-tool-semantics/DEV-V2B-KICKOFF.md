<!-- © 2026 Cartman ApS. All rights reserved. -->
# Kickoff Prompt: CLI + Skill v2b (answer-verification gate, synthesis-first)

**Status: proposed, alternative to DEV-V2-KICKOFF.md, not yet decided or
implemented.** This documents a second, materially different design change
derived from grading dev-v1 (see [RUNBOOK.md](./RUNBOOK.md)). It targets the
same 11 dev-v1 correctness failures as
[DEV-V2-KICKOFF.md](./DEV-V2-KICKOFF.md) but from a different root-cause
theory, and proposes a different fix axis. The two are not mutually
exclusive, but this document stands alone and does not assume v2's changes
are adopted. Do not implement until a decision is made between them (or to
merge them).

---

## Why (self-contained — restates the dev-v1 finding, then disagrees with v2's diagnosis)

dev-v1 scored 43/54 (79.6%) correct-or-correctly-refused, against
`skill-transfer`'s 54/54 (100%) on the identical 54 questions via raw
`redis-cli` Cypher (same skill, same graph, same model). DEV-V2-KICKOFF.md
reads this gap as a retrieval-tool problem and fixes the query surface
(cypher-first, pre-flight schema-shape check, Cypher examples). This
document reads the same evidence differently.

**RUNBOOK.md's own failure-pattern-analysis already states the conclusion
this design acts on:** "this run's failures sit mostly downstream of correct
retrieval, in how the agent summarized what it already had... does not
appear to be a CLI-selection problem." Three observations support treating
that sentence as load-bearing rather than a side remark:

1. **The 3 command-selection defects (SA-E2, AU-H1, PM-E3) were all
   already-correct answers** (✅⚠ in the per-question table) — fixing them
   moves the command-selection discipline metric, not the 79.6% correctness
   score. v2's pre-flight schema check targets exactly these 3, and its own
   scope check admits as much.
2. **Of the 11 real correctness failures, only the 3 miscounting cases
   (AU-M2, SEC-M3, EM-H2) are even plausibly addressed by v2's cypher-first
   `count()` argument.** The other 8 — refusal-discipline slippage (RM-E2,
   EM-M3) and dropped rubric points/citations (SA-H2, RM-H2, PM-H1, PM-H2,
   SEC-E1, SWE-H1) — are acknowledged by v2's own scope check as out of
   reach for that design.
3. **`.github/skills/ps-domain/SKILL.md` already contains written rules for
   the exact failure modes in classes 1, 3, and 4** — rule 3 ("account for
   every row returned"), rule 8 ("unit-of-counting discipline"), rule 4
   (governance-status caveats), rule 5 (cite real IDs). These rules existed
   in dev-v1 and were violated anyway. That is evidence against "add more
   prose rules" (v2's design change 3) closing the gap, and evidence for a
   missing enforcement step rather than a missing instruction.

**The alternative theory this document acts on:** the agent retrieves
correct data (confirmed independently for 3/3 miscounting cases and 5/5
dropped-rubric cases — the underlying facts were right) and mostly knows the
applicable rule, but nothing in the process forces it to check a drafted
answer against the retrieved data or the rule set before submitting. Fix the
missing check, not the query surface.

**Scope check — what this change does and does not target:** it aims at all
11 correctness failures (unlike v2's explicit 3/11), but with uneven
confidence. It has a direct, structural mechanism for classes 1
(miscounting) and 2 (refusal-discipline slippage), a checklist-shaped
mechanism for class 3/4 (dropped rubric points, missing IDs) that depends on
checklist coverage the same way v2's examples depend on example coverage,
and it does **not** address Cypher-shape fabrication at all — that failure
mode (fabricated relationship types, wrong properties, reversed direction)
is a legitimate, separate problem that v2's pre-flight schema check solves
better than anything proposed here. Do not expect dev-v2b to close the
shape-error discipline gap; track that separately if both designs are ever
merged.

## Design change 1 — CLI: leave the command surface alone

- **Keep** `ps query template`, `ps query catalog`, `ps templates`, and
  `ps cypher` exactly as they are in CLI v1. This is the direct opposite of
  v2's design change 1.
- **Rationale**: dev-v1 measured 0/54 freelancing and 0/54
  parameter-guessing on this command surface. The routing layer is not
  broken — rebuilding it (v2's approach) spends effort on a part of the
  system dev-v1's own data says is already working, and gives up AD-3's
  bounded/computed-surface property in exchange for a general-purpose query
  language plus a compensating safety net.

## Design change 2 — CLI: one small, additive output enrichment

- Add a top-level `"row_count": N` field to every command's
  `--format json` output (`query template`, `query catalog`, `cypher`),
  computed by `ps.py` itself from the result set, not by the agent counting
  rows.
- This is the one place this design concedes v2's underlying instinct — a
  tool-computed number beats agent arithmetic over a JSON blob — but
  implements it by enriching the existing deterministic commands rather than
  removing them in favor of cypher-first `count()`. Any "how many" question
  then has a single canonical, tool-sourced number to check a draft answer
  against, regardless of which command answered it.
- Scope: additive field only, no change to existing output shape, no new
  subcommand, no removal of `query template`/`query catalog`.

## Design change 3 — Skill: mandatory pre-submit verification block

Add a required structured self-check to `ps-domain/SKILL.md`, run by the
agent after drafting an answer and before submitting it — not a new tool
call, a required reasoning step the skill instructs the agent to perform
explicitly in its own output. Four parts, each aimed at one dev-v1 failure
class:

1. **Requirements restatement.** Before drafting, state what the question's
   phrasing demands: an exact count, an exhaustive list, a specific ID, a
   comparison, a refusal-check. Written down before the answer, so the
   verification step below has something concrete to check against.
2. **Recount check.** For every number in the draft answer, recompute it
   against the retrieved rows or the new `row_count` field (design change 2)
   and confirm the two match before finalizing. → targets class 1
   (AU-M2, SEC-M3, EM-H2— all three stated a number that didn't match data
   already in hand).
3. **Completeness check.** For every entity/chain cited, confirm the
   governance-status caveat and real ID are present, checked directly
   against SKILL.md rules 4/5/6. → targets class 3/4 (SA-H2, RM-H2, PM-H2,
   SEC-E1, SWE-H1).
4. **Known-gaps lookup.** Check the draft against a new, hardcoded
   **Known-Gaps Registry** (see design change 3b below) *before* answering
   or refusing. → targets class 2 (RM-E2, EM-M3).

### Design change 3b — Skill: Known-Gaps Registry

Add an explicit, hardcoded list to `SKILL.md` derived from FINDING-001's
confirmed dataset gaps: GDPR Art. 83 fine figures, NIS2 Art. 23.3
enforcement text, the specific figure EM-M3 searched for externally, and CRA
fine/penalty text — anything already confirmed absent from the graph across
both `skill-transfer` and dev-v1.

- Converts "search exhaustively, then maybe give up" (fragile — this
  specific pattern is what produced EM-M3's escalation to an external
  web-search tool after exhausting the graph) into "check the registry
  first, then refuse with confidence" (deterministic, and short-circuits the
  search effort that preceded both RM-E2's wrong-focus answer and EM-M3's
  escape).
- Keep the registry scoped to *confirmed* gaps only — do not pre-emptively
  list suspected gaps, which would risk teaching premature refusal on
  questions that are actually answerable.

## Design change 4 — Harness: lock every tool class, not just shell

- Audit `run_dev_set.sh`'s allow-tool scoping to confirm it blocks every
  tool class available to the harness, not only `shell` — EM-M3 reached an
  MCP web-search tool that shell-only scoping did not cover.
- This closes the EM-M3 escape at the access-control layer, independent of
  the skill wording change in 3b. Treat this as complementary to 3b, not a
  substitute for it — 3b prevents the *reason* to escape, this prevents the
  *ability* to.
- If the harness can't fully lock every tool class down, note it as a known
  gap rather than silently re-running under the same exposure (same
  discipline v2 already calls for).

## Procedure for the dev-v2b run

1. Implement design changes 1–4 above in `ps.py` and `SKILL.md`.
2. Reuse the **same 54 dev-set questions** verbatim from
   [run_dev_set.sh](./run_dev_set.sh) — do not reword, re-tier, or drop any.
3. Run the harness exactly as `run_dev_set.sh` does (same model, `kimi-k3`,
   same reference-date anchor), with the audited allow-tool scoping from
   design change 4.
4. Output to `spikes/cli-tool-semantics/runs/dev-v2b/`, one transcript per
   question, same skip-if-exists behavior as `run_dev_set.sh`.
5. Grade against [dev-answers.md](../../docs/test-data/dev-answers.md), same
   discipline as dev-v1's grading (see RUNBOOK.md's "Grading Bar").
   Explicitly re-check all 11 dev-v1 correctness failures (AU-M2, SEC-M3,
   EM-H2, RM-E2, EM-M3, SA-H2, RM-H2, PM-H1, PM-H2, SEC-E1, SWE-H1) to see
   which classes this design actually resolved, and separately re-check the
   3 command-selection-only defects (SA-E2, AU-H1, PM-E3) with the
   expectation that this design does **not** fix them (no schema-shape
   check here) — confirming that is itself useful signal.
6. Record results in RUNBOOK.md as a new `dev-v2b` results section (don't
   overwrite dev-v1's or a future dev-v2's — keep all for comparison), and
   update README.md's status line.

## Discipline rules

- Held-out set: untouched. This is a dev-only iteration.
- Question catalog: untouched — reused verbatim per step 2 above.
- Known-Gaps Registry (3b): additions require a confirmed absence (a real
  query that returned zero rows after schema-verified retry, or an existing
  FINDING-001 entry) — never add a suspected gap speculatively.
- If the verification block (design change 3) produces false
  positives — flags a draft answer as incomplete when it was actually
  correct — record and fix the checklist wording, don't drop the
  requirement to run it.
- Log this as a changelog entry the way `skill-transfer` tracks skill
  versions against the failures they addressed.

## Deliverable

- `ps.py` v2b (command surface unchanged from v1, `row_count` field added to
  JSON output — no commands removed, no pre-flight schema check).
- `SKILL.md` update (pre-submit verification block + Known-Gaps Registry
  added; CLI Command Surface section and rule 9 unchanged from v1).
- Harness allow-tool audit covering all tool classes.
- A `dev-v2b` run and grading, with an explicit before/after comparison
  against all 11 dev-v1 correctness failures and the 3 command-selection
  defects.
- An updated verdict on AD-3 at the CLI boundary, stated independently of
  DEV-V2-KICKOFF.md's verdict so the two can be compared before either is
  adopted.
