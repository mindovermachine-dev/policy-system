# © 2026 Cartman ApS. All rights reserved.
"""Claim-schema adapter (README.md gap 2, PROGRESS.md D1) -- dispatches one
question's `ClaimSet` to the `fitness.py` check(s) its `ClaimKind` maps to,
producing the `FitnessResult` `compose.compose_output` needs.

Only the checks a question's `RoutingDecision.mandatory_check_names` (or,
for DIRECT_MANDATORY_CHECK with an empty set, any claim present) actually
calls for are run -- mirrors `tests/run_target_cases.py`'s discipline of
never composing a check that wasn't asked for, and keeps this adapter from
becoming a run-everything shortcut that would mask the same vacuous-pass
gap `compose.py` already closes on the routing side.

CITED_IDS is the one claim kind that needs an "independent_query" `types.py`
calls pipeline-supplied context -- a query the adapter must construct, not
something the harness may pass in (letting the harness choose its own
independent query would let it grade its own homework, same reasoning
`types.py`'s docstring gives for capability_id). v0's only supported shape:
Obligations required by the claim's `capability_id`
(`MATCH (o:Obligation)-[:REQUIRES]->(c:Capability {id: ...}) RETURN o.id`) --
the same anchor-at-capability pattern `fitness.py` already uses for SEC-H4.
A CITED_IDS claim about control or requirement ids is a named, visible gap
in v0, not a silent misfire: `dispatch_claims` raises rather than run the
wrong independent query against the wrong id kind.
"""

from __future__ import annotations

from typing import List

from . import fitness
from .types import Claim, ClaimKind, ClaimSet, FitnessResult, Stage1Result


class AdapterError(ValueError):
    """A claim can't be dispatched -- missing a required field, or a
    CITED_IDS claim outside v0's supported (capability-anchored,
    obligation-id) shape. Raised rather than silently skipped: a missing
    check is exactly the vacuous-pass hole `routing.py`/`compose.py` exist
    to close, so an adapter that swallowed it would reopen that hole one
    layer up.
    """


def _cited_ids_independent_query(capability_id: str) -> str:
    return "MATCH (o:Obligation)-[:REQUIRES]->(c:Capability {id: '" + capability_id + "'}) RETURN o.id"


def _dispatch_one(claim: Claim, reference_date: str, stage1: Stage1Result) -> List:
    if claim.kind == ClaimKind.OVERDUE_SET:
        return [fitness.check_overdue_excludes_deprecated(reference_date, claim.control_ids)]

    if claim.kind == ClaimKind.CITED_IDS:
        if claim.capability_id is None:
            raise AdapterError("CITED_IDS claim missing capability_id -- required to build the independent query")
        independent_query = _cited_ids_independent_query(claim.capability_id)
        return [
            fitness.check_existence(claim.ids, independent_query),
            fitness.check_completeness(claim.ids, independent_query),
        ]

    if claim.kind == ClaimKind.REGULATION_SCOPE:
        if claim.capability_id is None:
            raise AdapterError("REGULATION_SCOPE claim missing capability_id")
        return [fitness.check_regulation_scope(claim.capability_id, claim.regulations)]

    if claim.kind == ClaimKind.COUNTING_UNIT:
        return [fitness.check_entity_type_match(stage1.entity_type, claim.entity_type)]

    if claim.kind == ClaimKind.FANOUT_MAXIMUM:
        if claim.capability_id is None or claim.count is None:
            raise AdapterError("FANOUT_MAXIMUM claim requires both capability_id and count")
        return [fitness.check_fanout_maximum(claim.capability_id, claim.count)]

    if claim.kind == ClaimKind.EVIDENCE_GAP_CATEGORY:
        if claim.capability_id is None or claim.category is None:
            raise AdapterError("EVIDENCE_GAP_CATEGORY claim requires both capability_id and category")
        return [fitness.check_evidence_gap_root_cause(claim.capability_id, claim.category)]

    raise AdapterError(f"unhandled claim kind {claim.kind!r}")


def dispatch_claims(claim_set: ClaimSet, stage1: Stage1Result, reference_date: str) -> FitnessResult:
    """Run every claim in `claim_set` through its matching fitness.py
    check(s) and collect all resulting `CheckResult`s. Dispatches every
    claim present, unconditionally -- whether that's *enough* to satisfy a
    question's `RoutingDecision.mandatory_check_names` is `compose.py`'s
    gate to judge, not this adapter's; duplicating that gate here would
    risk silently dropping a legitimately-run check's evidence from (C)
    (e.g. CITED_IDS's `check_completeness` half, when only
    `existence_grounding` happens to be named mandatory).
    """
    checks = []
    for claim in claim_set.claims:
        checks.extend(_dispatch_one(claim, reference_date, stage1))
    return FitnessResult(question_id=claim_set.question_id, checks=checks)
