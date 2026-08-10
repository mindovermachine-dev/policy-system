<!-- © 2026 Cartman ApS. All rights reserved. -->
# Progress Tracker — E2E Pipeline Implementation

Resumable build state for [README.md](./README.md)'s design.

## Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Claim representation: harness emits structured claims alongside prose vs. parsing prose after the fact | **Resolved** — structured claims; schema below |
| D2 | Fork `pipeline/*` + `ps.py` into this spike; no live cross-spike import | **Resolved** — see [[spike-independence-no-shared-code]] |
| D3 | Harness wiring: extend `ps-domain/SKILL.md` directly vs. a separate addendum scoped to this spike | Open |
| D4 | Relationship to `end-to-end-slice`: keep separate | **Resolved** |

### D1 detail — claim schema

`compliance-decision-pipeline`'s `pipeline/fitness.py` has no unified claim
type — each of its 7 Stage 4 checks takes its own ad hoc argument shape,
today hand-derived per test case by a human reading answer prose
(`tests/run_target_cases.py`). D1 replaces that human with the harness: one
claim kind per check, same "thin, not speculative" discipline `types.py`
already documents for itself.

| Claim kind | Fields (harness asserts) | Feeds |
|---|---|---|
| `overdue_set` | `control_ids: [str]` | `check_overdue_excludes_deprecated` |
| `cited_ids` | `ids: [str]` | `check_existence` **and** `check_completeness` (same claim, both directions) |
| `regulation_scope` | `regulations: [str]` | `check_regulation_scope` |
| `counting_unit` | `entity_type: str` | `check_entity_type_match` |
| `fanout_maximum` | `capability_id: str, count: int` | `check_fanout_maximum` |
| `evidence_gap_category` | `category: governance\|engineering\|resolved\|excluded` | `check_evidence_gap_root_cause` |

**Claim vs. context.** Not every fitness.py parameter is a claim to verify.
`reference_date`, `independent_query`, and (usually) the anchor
`capability_id` are pipeline-supplied context for constructing the
independent re-query — letting the harness supply those would be letting it
grade its own homework. `fanout_maximum` is the exception: both fields are
genuinely claimed (the answer asserts *which* capability *and* its count).

**Capability-id gap surfaced by this decision.** Several checks need a
`capability_id` anchor that no Stage 1 mechanism extracts from live question
text (Stage 1 only gives `entity_type` and term matches). Resolution: the
claim payload always carries the `capability_id`(s) the answer's evidence
was drawn from — the harness already has this from whatever `ps query
catalog` / `ps capabilities list` call it made — rather than having the
adapter re-derive it from question text.

**Emission mechanism (tool arg vs. CLI flag vs. fenced block) is D3's
concern, not D1's** — this schema is the payload shape, independent of how
it reaches the CLI.

## Environment

- FalkorDB at `localhost:6379`, graph `policy_system`, via `/usr/bin/python3` (not repo `.venv`)
- Query surface + verification logic: this spike's own forked copies (D2)
- `ps-domain` skill and FalkorDB itself stay shared — not spike-local

## Build status

| Component | Status |
|---|---|
| README.md | ✅ |
| PROGRESS.md | ✅ |
| D1 | ✅ resolved — schema above |
| D3 | ⬜ open |
| Fork `pipeline/*` + `ps.py` | ✅ — plus `query_mechanism_v1.py` + `catalog.py`, ps.py's real dependency closure (see below) |
| `Claim`/`ClaimSet` types (`pipeline/types.py`) | ✅ |
| CLI entrypoint | ⬜ |
| Claim-schema adapter (dispatch `ClaimSet` → fitness.py calls per `RoutingDecision`) | ⬜ unblocked — D1 resolved |
| Harness-side wiring | ⬜ depends on D3 |
| First live-question batch | ⬜ |
| Failure-kind comparison | ⬜ |
| AD-7 verdict | ⬜ |

Fork note: `ps.py` imports `query_mechanism_v1` and `catalog` directly (not
just `pipeline/*`), so those two files were forked alongside it, and every
`sys.path` hack toward sibling `query1`/`query2` directories was removed
(including a dead one in `catalog.py`, confirmed unused before deleting) —
otherwise the "fork" would still import live across the spike boundary at
runtime. Verified with a stubbed `falkordb` module: full import graph
resolves with zero `spikes/query1`/`spikes/query2` entries on `sys.path`.

## How to run

Not runnable yet — CLI entrypoint and claim-schema adapter don't exist. The
forked `pipeline/*` and `ps.py` import cleanly (`/usr/bin/python3`, no
FalkorDB required for import) but nothing wires them together yet.

## Next action

Resolve D3, build the CLI entrypoint, then the claim-schema adapter (now
unblocked by D1).
