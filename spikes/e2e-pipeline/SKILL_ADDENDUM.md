<!-- © 2026 Cartman ApS. All rights reserved. -->
---
name: e2e-pipeline-addendum
description: >-
  Spike-local addendum (D3) grounding the harness in pipeline_cli.py's
  command shape -- routes every PS compliance answer through the
  verification pipeline before it reaches the user. Not yet folded into
  ps-domain/SKILL.md.
---

# E2E Pipeline Addendum (spike-local, D3)

Status: spike-local, not folded into `.github/skills/ps-domain/SKILL.md` yet
(README.md "Gaps to close" #3, PROGRESS.md D3). Once this spike's live-question
batch proves the loop, its content moves there — read it as a temporary
extension of that skill, not a replacement.

## When this applies

Every question answered against the `policy_system` graph via
`ps-domain/SKILL.md` — same scope as that skill. After answering normally
(its tools, its rules), verify the answer through this spike's pipeline
before presenting it.

## Procedure

1. Answer the question using `ps-domain/SKILL.md` as usual (schema, ID
   conventions, CLI command surface, Pre-Submit Verification).
2. Assign the question a type, A-H (`pipeline/question_types.py`) —
   first-pass judgment call, no classifier exists.
3. Extract structured claims for what the answer actually asserts — one
   `Claim` per Stage 4 check kind it triggers (table below). Omit a claim
   rather than force-fitting one that doesn't match — an omitted claim is an
   honest "no check ran here," not a silent pass (`compose.py`'s
   no-false-auto-pass discipline).
4. Carry the `capability_id`(s) the answer's evidence was drawn from into
   the claim payload — from whatever `ps query catalog` /
   `ps capabilities list` call was already made during step 1. Do not let
   the adapter guess it from question text; it can't.
5. Call the pipeline CLI (command shape below).
6. Present the CLI's three-block output verbatim — (A) confidence, (B)
   answer, (C) verification data. Never present a bare answer instead.

## Claim schema (PROGRESS.md D1)

| Claim kind | Fields you supply | Verifies |
|---|---|---|
| `overdue_set` | `control_ids` | Deprecated controls wrongly included in an "overdue" set |
| `cited_ids` | `capability_id`, `ids` | Existence + completeness of cited Obligation ids (v0: capability-anchored Obligations only — see Known gaps) |
| `regulation_scope` | `capability_id`, `regulations` | A regulation claimed as routing through a capability actually does |
| `counting_unit` | `entity_type` | Answer counted in the unit the question asked about |
| `fanout_maximum` | `capability_id`, `count` | A "most obligations require X" ranking claim |
| `evidence_gap_category` | `capability_id`, `category` | governance/engineering/resolved/excluded classification |

**A type-C count answer usually needs `counting_unit` *in addition to*
whatever domain-specific claim it also triggers.** Routing requires
`entity_type_cross_check` whenever Stage 1 records an entity type from the
question text — separately from any rule/scope/root-cause claim the count
itself is about. Supplying only the domain-specific claim (e.g. `overdue_set`
for "how many Controls are overdue") leaves that mandatory check unmet and
gets an otherwise-correct answer flagged (confirmed live, PROGRESS.md
"Second live-question batch," EM-M1).

**Two known claim-schema gaps — no fitting claim kind exists yet, don't
force one:**
- A *which-capabilities* question filtered by a structural predicate
  ("zero implemented Controls," "required by more than one Obligation") —
  `cited_ids`' independent query only grounds an obligation-list anchored at
  one already-known capability, not a capability-set enumeration itself.
- A *single, non-exhaustive chain-trace* claim ("trace this one path") —
  every existing check's independent query is scoped to completeness/
  existence over a whole set, so it would misrepresent a narrowly-scoped
  trace as an exhaustiveness claim.

Both confirmed live (PROGRESS.md, Findings 1 and 2) — a question hitting
either shape gets an honest "not verified" flag with zero claims, not a
false pass. State the gap plainly rather than reshaping the claim to force
a check that doesn't fit.

Do not supply `reference_date` or the independent re-query yourself —
pipeline-constructed context, not a harness claim (`types.py`'s `Claim`
docstring: letting the harness supply those is letting it grade its own
homework).

## Command shape

```
/usr/bin/python3 spikes/e2e-pipeline/pipeline_cli.py query "<question, verbatim>" \
  --type <A-H> \
  --answer "<answer prose>" \
  --claims '{"claims": [{"kind": "overdue_set", "control_ids": ["ctrl_..."]}]}' \
  [--reference-date YYYY-MM-DD] [--format text|json]
```

Must run under `/usr/bin/python3`, not the repo `.venv` — that interpreter
lacks `falkordb` (PROGRESS.md Environment).

## Known v0 gaps (state these, don't paper over them)

- `cited_ids` only supports ids anchored at a single Capability's required
  Obligations. A claim about Control or Requirement ids raises
  (`AdapterError`), not silently misfires — if hit, say so plainly rather
  than reshaping the claim to fit.
- No check exists yet for "stale" chains or tool-computed Miscount
  elimination — a type-B/C/D question that needs one gets an honest "no
  mandatory check performed" flagged result, not a vacuous pass.
- Routing-table blind spots (unseen signal combinations) are inherited from
  `compliance-decision-pipeline`, not closed by this spike.

## Not this addendum's job

- Deciding *whether* to answer at all (that's `ps-domain/SKILL.md`'s
  Pre-Submit Verification and Known-Gaps Registry, unchanged).
- Producing the answer prose — this addendum verifies a candidate answer, it
  does not compose one.
