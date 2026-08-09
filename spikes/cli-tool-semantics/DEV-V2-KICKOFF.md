<!-- © 2026 Cartman ApS. All rights reserved. -->
# Kickoff Prompt: CLI + Skill v2 (cypher-first, schema-validated)

**Status: proposed, not yet decided or implemented.** This documents a
design change to `ps.py` and `ps-domain/SKILL.md` derived from grading
dev-v1 (see [RUNBOOK.md](./RUNBOOK.md)), so it can be implemented in a
single pass — by this session or a fresh one — if and when the change is
approved. Do not implement until that decision is made.

---

## Why (self-contained — restates the dev-v1 finding)

dev-v1 scored 43/54 (79.6%) correct-or-correctly-refused, against
`skill-transfer`'s 54/54 (100%) on the identical 54 questions via raw
`redis-cli` Cypher (same skill, same graph, same model). Two things were
true about dev-v1 that motivate this change:

1. **The fixed deterministic commands (`ps query template`,
   `ps query catalog`) never caused a wrong answer through bad command
   selection** — zero freelancing, zero parameter-guessing, across all 54
   runs. But `ps query template` returns raw rows, not computed answers, so
   count-shaped questions forced the agent to hand-summarize CLI output —
   a failure class (AU-M2, SEC-M3, EM-H2 — 3/11 failures) that never
   occurred in `skill-transfer`'s dev run, where raw Cypher's `count()` did
   the arithmetic instead.
2. **The only schema-shape Cypher errors seen anywhere in either spike**
   (SA-E2's fabricated `FROM_REGULATION` relationship type, AU-H1's
   reversed `SATISFIED_BY` direction, PM-E3's nonexistent `Policy.name`
   property — all self-corrected, all in dev-v1) happened when `ps cypher`
   was reached as a *fallback* after `query template`/`query catalog` had
   already been tried and failed. `skill-transfer` ran cypher as the
   *sole* tool across 108 dev+blind runs and had zero shape errors. That
   split (0/108 vs 3/54) suggests cypher-as-primary-tool gets applied more
   carefully than cypher-as-escape-hatch, independent of whether the
   schema is documented.

Neither finding indicts the deterministic-CLI-surface *concept* — it
indicts these two specific fixed commands for (a) stripping out the
aggregate computation raw Cypher had, and (b) demoting cypher to a
last-resort tool. The fix targets both directly, while keeping the
CLI-as-safety-boundary property AD-3 actually cares about (auditable,
no-arbitrary-write access) — which raw, ungated Cypher in a production
gateway would give up.

**Scope check — what this change does and does not target:** it addresses
the 3/11 dev-v1 failures that were schema-shape errors. It does **not**
target the other 8/11 (miscounted own-data summaries at the reasoning
layer beyond raw row counts, dropped rubric points, refusal-discipline
slippage including EM-M3's escape to an external web-search tool). Do not
expect dev-v2 to hit 100% from this change alone — track whether the
targeted 3 are eliminated, and treat any movement on the other 8 as a
side observation, not a claim.

## Design change 1 — CLI: drop the fixed answer commands, cypher-first

- **Remove** `ps query template` and `ps query catalog` as user-facing
  commands (they produce computed answers via a fixed mechanism — exactly
  the thing being dropped). Retire `ps templates` alongside `query
  template` since it only documents that command's patterns.
- **Keep** `ps capabilities list` — it's a vocabulary/discovery aid (labels
  and ids to write a *correct* cypher query with), not an alternate
  answer-producing path. It stays useful under a cypher-first design.
- **`ps cypher` becomes the primary (likely sole) query command.** Update
  `.github/skills/ps-domain/SKILL.md`'s "CLI Command Surface" section and
  rule 9 to remove the 3-step routing order (template → catalog → cypher)
  and describe `ps cypher` as the default mechanism, with
  `ps capabilities list` as the recommended first step for any question
  naming a Capability, Policy, Standard, or Control by name (resolve the
  id/vocabulary before writing the query, not instead of it).

## Design change 2 — CLI: deterministic pre-flight schema-shape check on `ps cypher`

**Constraint that shapes this:** an unknown label, relationship type, or
property in FalkorDB/openCypher is not a runtime error — it silently
matches zero rows. That's why these are a "silent-error class" and why a
try/except around execution can't catch them; the check has to be
pre-flight and structural, mirroring how `ps.py`'s existing
`_WRITE_CLAUSE` regex guard rejects write clauses before execution.

**Phase 1 (build this first) — existence-checking:**
1. Generate a schema manifest once per connection — labels,
   relationship types, and per-label property sets — via FalkorDB's schema
   introspection procedures (`CALL db.labels()`, `CALL db.relationshipTypes()`,
   `CALL db.propertyKeys()`, or the per-label property equivalent). Pull
   this from the live graph, not a hand-maintained list in code, so it
   can't drift from what the graph actually contains.
2. Before executing a `ps cypher` query, extract every `:Label`,
   `-[:REL_TYPE]-`, and `.property` token from the query text (a
   lightweight regex/token pass is enough for phase 1 — no full Cypher
   parser needed, consistent with `_WRITE_CLAUSE`'s existing approach).
3. Reject (same style as the write-clause guard: print an error, exit
   non-zero, do not execute) any query referencing a label, relationship
   type, or property not in the manifest. The error message should name
   the real vocabulary near the invalid token (e.g. "no relationship type
   `FROM_REGULATION` — did you mean one of: HAS, REQUIRES, GOVERNED_BY,
   ..."), not just say "invalid."
4. This alone would have caught 2 of the 3 dev-v1 shape errors
   (`FROM_REGULATION`, `Policy.name`) before execution.

**Phase 2 (stretch, only if phase 1 proves out) — direction-checking:**
Encode the schema as typed `(start-label, rel-type, end-label)` triples
(derivable from `ps-domain-concepts.md`, the canonical domain-model doc)
and check the query's actual traversal direction against valid triples.
This is meaningfully more engineering than phase 1's flat vocabulary
check and would only have caught 1 of the 3 dev-v1 errors (AU-H1's
reversed `SATISFIED_BY`) — build phase 1 first and decide separately
whether phase 2 is worth it.

## Design change 3 — Skill: good/bad Cypher examples

Add a small set of worked examples to `ps-domain/SKILL.md`, paired with
(not replacing) the existing schema table. Each example should show a
plausible-but-wrong query and the fix, scoped to teach the *category* of
mistake, not memorize the specific dev-v1 instances:

- Inventing a relationship type that sounds right but isn't in the schema
  — the fix is "verify against the schema table (or run a discovery query)
  before using an edge type you're not certain of," not "never use
  `FROM_REGULATION`."
- Assuming a property exists on a label because it exists on a similar
  label elsewhere in the schema (the `Policy.name` case — Policy doesn't
  carry `name`, other labels do).
- A reversed-direction traversal that reads naturally in English but is
  backwards in the schema.

Do not phrase examples around the exact dev-v1 IDs/strings — the point is
transferable discipline, not memorized answers to known failures (same
principle the project already applies to keeping the held-out set
uncontaminated by dev-set specifics).

## Procedure for the dev-v2 run

1. Implement design changes 1–3 above in `ps.py` and `SKILL.md`.
2. Reuse the **same 54 dev-set questions** verbatim from
   [run_dev_set.sh](./run_dev_set.sh) — do not reword, re-tier, or drop
   any. This is a CLI/skill change under test, not a question-catalog
   change.
3. Run the harness exactly as `run_dev_set.sh` does (same model,
   `kimi-k3`, same reference-date anchor, same allow-tool scoping to
   `ps.py` only — and re-verify the allow-tool actually blocks *every*
   other tool class this time, not just `shell`, given EM-M3's escape to
   an MCP web-search tool in dev-v1. If the harness can't fully lock that
   down, note it as a known gap rather than silently re-running under the
   same exposure).
4. Output to `spikes/cli-tool-semantics/runs/dev-v2/`, one transcript per
   question, same skip-if-exists behavior as `run_dev_set.sh`.
5. Grade against [dev-answers.md](../../docs/test-data/dev-answers.md),
   same discipline as dev-v1's grading (see RUNBOOK.md's "Grading Bar").
   Explicitly re-check the 11 dev-v1 failure IDs first (SA-H2, AU-M2,
   AU-H1, RM-E2, RM-H2, PM-H1, PM-H2, SWE-H1, SEC-E1, SEC-M3, EM-M3, EM-H2
   — note: 12 listed, AU-H1 was a command-selection-only fail, correctness
   passed) to see which failure classes the change actually resolved.
6. Record results in RUNBOOK.md as a new `dev-v2` results section
   (don't overwrite dev-v1's — keep both for the before/after comparison),
   and update README.md's status line.

## Discipline rules

- Held-out set: untouched. This is a dev-only iteration.
- Question catalog: untouched — reused verbatim per step 2 above.
- If phase 1's existence-check produces false positives (rejects a
  legitimate query), record and fix the manifest/extraction logic, don't
  loosen the check to "warn only" as a first resort — the whole point is a
  pre-flight guarantee, not another self-correctable soft signal.
- Log this as a changelog entry the way `skill-transfer` tracks skill
  versions against the failures they addressed.

## Deliverable

- `ps.py` v2 (cypher-first, `query template`/`query catalog`/`templates`
  removed, pre-flight schema-shape check added).
- `SKILL.md` update (CLI Command Surface section + rule 9 rewritten for
  cypher-first; good/bad Cypher examples added).
- A `dev-v2` run and grading, with an explicit before/after comparison
  against the 11 dev-v1 failures.
- An updated verdict on AD-3 at the CLI boundary.
