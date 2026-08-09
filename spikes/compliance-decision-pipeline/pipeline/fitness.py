# © 2026 Cartman ApS. All rights reserved.
"""Stage 4 -- fitness gate sub-checks (README.md, "Stage 4").

Each function here independently re-queries the graph through
ps_client.py -- never reuses whatever query produced the answer under
test. Four sub-checks implemented in v0, each validated against its own
named target case (see PROGRESS.md):

- check_overdue_excludes_deprecated  -- rule check (SEC-M2/SEC-M4)
- check_regulation_scope             -- scope-match, routing variant (AU-H4, SA-H1)
- check_entity_type_match            -- entity-type cross-check (EM-E3)
- check_existence                    -- existence grounding (SEC-E1, SEC-H1, CO-H2, SEC-H4)
- check_fanout_maximum               -- ranking grounding (SA-H2)
- check_evidence_gap_root_cause      -- root-cause classification (EM-M4)
- check_completeness                 -- completeness grounding (CO-M2)

Design finding (later session, live held-out audit): "existence grounding"
turned out not to be one mechanism either. check_existence only checks
`claimed - retrieved` -- does every claimed id actually exist? -- which
catches fabrication/over-claiming but is structurally blind to omission: a
claimed set that is a real, non-fabricated *subset* of the truth passes it
cleanly, no matter how incomplete. This was found, not assumed: a live
held-out audit (PROGRESS.md) fed CO-M2's actual failing answer (15 of the
real 24 obligations, missing 9) through the unmodified check_existence and
confirmed it does not flag -- every one of the 15 claimed ids is real, so
there's nothing for existence grounding to catch. check_completeness below
checks the other direction, `retrieved - claimed`. IMPORTANT CAVEAT: unlike
every other mechanism in this file, check_completeness was designed using
CO-M2 as direct reference (that's how the gap was found and the fix
motivated) -- it is NOT held-out-validated the way check_existence's
original four target cases were. CO-M2 is now spent as future held-out
data for this file; see PROGRESS.md's live audit section for the full
discipline note.

Design finding (earlier session): "granularity precision" turned out not to
be one mechanism either, same pattern as scope-match's AU-H4/SEC-H4 split
below. EM-E3's granularity-slip is a counting-*unit* mismatch (chain vs.
control) -- check_entity_type_match's shape. EM-M4's is a root-*cause*
mismatch (governance vs. engineering) -- a different dimension entirely,
not reducible to a counting unit, so forcing it through
check_entity_type_match would have been a false fit (PROGRESS.md's prior
"don't force a fit" call on EM-M4 was correct at the time -- no mechanism
for this dimension existed yet). check_evidence_gap_root_cause below is
that mechanism, added once a real, independently-re-derivable structural
definition was found: see its own docstring.

Revision to the earlier note here (see PROGRESS.md "Design finding,
revisited"): SEC-H4 was originally flagged as needing a bespoke
"redundancy-aware" mechanism distinct from AU-H4's, on the theory that
CRA/NIS2/GDPR each genuinely have *some* obligation requiring the
Encryption-at-Rest control's capability, so a regulation-level routing
check couldn't catch the over-claim. Re-derived against live data this
session: the actual RUNBOOK-recorded failure ("listed duties verified by
the v2/v3 controls, not failing on Aug 15") is an over-claim at
*obligation* granularity, not regulation granularity -- the failing
answer cited MFA/logging obligations that require different capabilities
entirely (cap_access_control_authentication, cap_security_logging), not
cap_data_encryption. check_existence, scoped to the failing control's own
capability instead of the whole policy's capability set, catches this
directly -- no new function needed, just the right independent query
(see tests/fixtures.py's DATA_ENCRYPTION_* constants). SA-H1 similarly
reuses check_regulation_scope unchanged (it is a routing over-claim,
same shape as AU-H4, on a different capability). SA-H2 is the one
genuinely new shape: a ranking claim ("X is required by the most
obligations"), not a membership claim -- check_existence's shape doesn't
fit, hence check_fanout_maximum below.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Set

from . import ps_client
from .types import CheckResult


def check_overdue_excludes_deprecated(
    reference_date: str, answer_control_ids: Iterable[str]
) -> CheckResult:
    """Rule check: an answer's "overdue" set must exclude deprecated
    Controls even when their next_review_date has also passed (SKILL.md's
    Overdue definition). Targets SEC-M2/SEC-M4's failure: both included a
    deprecated control in the overdue bucket, caveated but not excluded --
    golden requires exclusion, not exclusion-with-caveat.
    """
    answer_set = set(answer_control_ids)

    deprecated_past_due = ps_client.cypher(
        "MATCH (c:Control) WHERE c.next_review_date < '"
        + reference_date
        + "' AND c.implementation_status = 'deprecated' RETURN c.id"
    )
    deprecated_ids = {row[0] for row in deprecated_past_due["rows"]}

    violating = answer_set & deprecated_ids
    if violating:
        return CheckResult(
            check_name="rule_overdue_excludes_deprecated",
            flagged=True,
            reason=(
                f"answer's overdue set includes deprecated control(s) "
                f"{sorted(violating)} -- SKILL.md's Overdue definition "
                f"excludes deprecated Controls regardless of review date"
            ),
            evidence={"deprecated_past_due_ids": sorted(deprecated_ids), "answer_set": sorted(answer_set)},
        )
    return CheckResult(
        check_name="rule_overdue_excludes_deprecated",
        flagged=False,
        reason="no deprecated control present in the answer's overdue set",
        evidence={"deprecated_past_due_ids": sorted(deprecated_ids), "answer_set": sorted(answer_set)},
    )


def canonical_overdue_set(reference_date: str) -> Set[str]:
    """The ground-truth overdue set per SKILL.md's definition -- past
    next_review_date, excluding deprecated. Exposed separately so tests
    and callers can compare an answer's claimed set against it directly,
    not just probe for the deprecated trap.
    """
    result = ps_client.cypher(
        "MATCH (c:Control) WHERE c.next_review_date < '"
        + reference_date
        + "' AND c.implementation_status <> 'deprecated' RETURN c.id"
    )
    return {row[0] for row in result["rows"]}


def check_regulation_scope(
    capability_id: str, claimed_regulations: Iterable[str]
) -> CheckResult:
    """Scope-match (routing variant): a claim naming a regulation as
    affected via a specific capability must be backed by an actual
    obligation-to-capability edge for that regulation. Targets AU-H4:
    the failing answer claimed NIS2/GDPR "weakened" via the shared
    standard the log-retention capability's controls sit under, but
    neither regulation has any obligation requiring this capability at
    all -- SKILL.md rule 7 (narrowing): cite only chains that route
    through the named node, not siblings reached via a shared
    downstream node.
    """
    catalog = ps_client.query_catalog(capability_id)
    routed_regulations = {row[0] for row in catalog["rows"]}

    claimed = set(claimed_regulations)
    over_claimed = claimed - routed_regulations
    if over_claimed:
        return CheckResult(
            check_name="scope_match_regulation_routing",
            flagged=True,
            reason=(
                f"claim names {sorted(over_claimed)} as affected via "
                f"{capability_id}, but no obligation from "
                f"{sorted(over_claimed)} requires this capability -- "
                f"only {sorted(routed_regulations)} actually route through it"
            ),
            evidence={"routed_regulations": sorted(routed_regulations), "claimed": sorted(claimed)},
        )
    return CheckResult(
        check_name="scope_match_regulation_routing",
        flagged=False,
        reason="every claimed regulation has a real obligation routing through this capability",
        evidence={"routed_regulations": sorted(routed_regulations), "claimed": sorted(claimed)},
    )


def check_entity_type_match(stated_entity_type: Optional[str], answer_counting_unit: Optional[str]) -> CheckResult:
    """Entity-type cross-check: the unit the answer actually counted in
    must match the entity-type Stage 1 recorded from the question text.
    Targets EM-E3: question asks about "chains" (31 of 57), answer
    counted "controls" (3 of 57) -- right numbers, wrong granularity.
    """
    if stated_entity_type is None or answer_counting_unit is None:
        return CheckResult(
            check_name="entity_type_cross_check",
            flagged=False,
            reason="no entity-type recorded by Stage 1 or no counting unit stated -- nothing to cross-check",
            evidence={"stated_entity_type": stated_entity_type, "answer_counting_unit": answer_counting_unit},
        )
    if stated_entity_type != answer_counting_unit:
        return CheckResult(
            check_name="entity_type_cross_check",
            flagged=True,
            reason=(
                f"question asks about '{stated_entity_type}', answer counted "
                f"in '{answer_counting_unit}' -- granularity mismatch"
            ),
            evidence={"stated_entity_type": stated_entity_type, "answer_counting_unit": answer_counting_unit},
        )
    return CheckResult(
        check_name="entity_type_cross_check",
        flagged=False,
        reason="answer's counting unit matches the question's entity type",
        evidence={"stated_entity_type": stated_entity_type, "answer_counting_unit": answer_counting_unit},
    )


def check_existence(claimed_ids: Iterable[str], independent_query: str, id_column_index: int = 0) -> CheckResult:
    """Existence grounding: independently re-query the graph (a query the
    caller constructs fresh, not the one that produced the answer) and
    verify every ID the answer cites is actually present in that result.
    Generic by design -- validated concretely against SEC-E1 (exactly 2
    real control IDs under a named policy) and SEC-H1 (exactly 7 real
    obligation IDs converging on a named capability) in
    tests/test_stage4.py.
    """
    result = ps_client.cypher(independent_query)
    retrieved_ids = {row[id_column_index] for row in result["rows"]}
    claimed = set(claimed_ids)

    missing = claimed - retrieved_ids
    if missing:
        return CheckResult(
            check_name="existence_grounding",
            flagged=True,
            reason=f"claimed id(s) {sorted(missing)} not found in independent re-query",
            evidence={"retrieved_ids": sorted(retrieved_ids), "claimed": sorted(claimed)},
        )
    return CheckResult(
        check_name="existence_grounding",
        flagged=False,
        reason="every claimed id confirmed present in independent re-query",
        evidence={"retrieved_ids": sorted(retrieved_ids), "claimed": sorted(claimed)},
    )


def check_completeness(claimed_ids: Iterable[str], independent_query: str, id_column_index: int = 0) -> CheckResult:
    """Completeness grounding: the other direction from check_existence.
    Independently re-query the graph and verify every id the query
    actually returns was included in the claim -- catches omission
    (a real, non-fabricated but incomplete claimed set), which
    check_existence is structurally blind to (see this module's docstring
    for the live audit that found the gap). Targets CO-M2: golden is 24
    obligations at confidence 0.75/0.80; the failing answer found 15,
    missing 9 of the 0.80 band -- every claimed id was real, so only the
    completeness direction catches it.

    Same generic shape as check_existence deliberately -- a caller that
    wants both fabrication and omission caught runs both checks against
    the same independent_query, same as scope-match and existence-
    grounding already compose in the existing target cases.
    """
    result = ps_client.cypher(independent_query)
    retrieved_ids = {row[id_column_index] for row in result["rows"]}
    claimed = set(claimed_ids)

    omitted = retrieved_ids - claimed
    if omitted:
        return CheckResult(
            check_name="completeness_grounding",
            flagged=True,
            reason=f"independent re-query found real id(s) {sorted(omitted)} that the claim omitted",
            evidence={"retrieved_ids": sorted(retrieved_ids), "claimed": sorted(claimed)},
        )
    return CheckResult(
        check_name="completeness_grounding",
        flagged=False,
        reason="every id in the independent re-query is present in the claim -- nothing omitted",
        evidence={"retrieved_ids": sorted(retrieved_ids), "claimed": sorted(claimed)},
    )


def check_fanout_maximum(claimed_capability_id: str, claimed_count: int) -> CheckResult:
    """Ranking grounding: independently recompute, across every Capability,
    which one is required by the most Obligations and how many -- then
    compare against a claim of the form "X is required by the most
    obligations (N)". Targets SA-H2. Not existence grounding's shape (is
    a claimed id a member of a set) -- this is a claim about a maximum
    over the whole catalog, so the independent re-derivation has to
    recompute the ranking, not just look up membership.
    """
    result = ps_client.cypher(
        "MATCH (o:Obligation)-[:REQUIRES]->(c:Capability) "
        "RETURN c.id, count(o) AS n ORDER BY n DESC LIMIT 1"
    )
    actual_capability_id, actual_count = result["rows"][0]

    if claimed_capability_id != actual_capability_id or claimed_count != actual_count:
        return CheckResult(
            check_name="fanout_maximum",
            flagged=True,
            reason=(
                f"claimed '{claimed_capability_id}' is required by the most obligations "
                f"({claimed_count}), but independent re-query finds '{actual_capability_id}' "
                f"is actually the maximum, with {actual_count}"
            ),
            evidence={
                "actual_capability_id": actual_capability_id,
                "actual_count": actual_count,
                "claimed_capability_id": claimed_capability_id,
                "claimed_count": claimed_count,
            },
        )
    return CheckResult(
        check_name="fanout_maximum",
        flagged=False,
        reason="claimed maximum-fanout capability and count match independent re-query",
        evidence={"actual_capability_id": actual_capability_id, "actual_count": actual_count},
    )


_LIVE_CONTROL_STATUSES = {"implemented", "reviewed"}
_VALID_ROOT_CAUSE_CATEGORIES = {"governance", "engineering", "resolved", "excluded"}


def classify_evidence_gap_root_cause(capability_id: str) -> str:
    """Independently derive a capability's evidence-gap root cause from
    its Policy/Standard/Control structure -- four categories, mutually
    exclusive:

    - "excluded": the Policy is deprecated -- out of scope, same exclusion
      discipline as check_overdue_excludes_deprecated (a deprecated Policy
      left the review cycle, it didn't fail it).
    - "governance": no Policy at all, or the Policy is still in `draft` --
      the chain is broken not because a build is late, but because no
      current Control backs it and nothing has even been organizationally
      chartered to do so. Includes the "no Policy at all" case (the
      majority of GDPR-linked capabilities, verified live) as the extreme
      instance of this same category, not a separate one -- there is even
      less governance structure there, not a different kind of gap.
    - "engineering": the Policy has at least one live (implemented/
      reviewed) Control -- the chain is not stale -- but also at least one
      Control that is not live and not merely deprecated (e.g. `planned`)
      under the same Policy. Something is actively being built, just not
      finished -- a build/schedule problem, not an organizational one.
    - "resolved": the Policy has at least one live Control and nothing
      else in progress -- no evidence problem at all.

    Verified live against EM-M4's two named RUNBOOK.md entities (see
    tests/fixtures.py): the Clinical Data Integrity policy's DPIA
    capability classifies "governance" (draft policy, zero Controls); all
    three capabilities under the Incident/Vulnerability Response policy
    classify "engineering" (v1 Control implemented, v2 still `planned`).
    """
    result = ps_client.cypher(
        "MATCH (cap:Capability {id: '"
        + capability_id
        + "'}) OPTIONAL MATCH (cap)-[:GOVERNED_BY]->(p:Policy) "
        "OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(:Standard)-[:IMPLEMENTED_BY]->(c:Control) "
        "RETURN p.status, collect(DISTINCT c.implementation_status)"
    )
    # OPTIONAL MATCH means this always returns exactly one row for a real
    # capability id -- p.status/the collected list are simply empty/None
    # when there's no Policy (or no Standard/Control) to find.
    policy_status, raw_statuses = result["rows"][0]
    control_statuses = {s for s in (raw_statuses or []) if s is not None}

    if policy_status == "deprecated":
        return "excluded"

    has_live_control = bool(control_statuses & _LIVE_CONTROL_STATUSES)
    has_incomplete_control = bool(control_statuses - _LIVE_CONTROL_STATUSES - {"deprecated"})

    if not has_live_control:
        return "governance"
    if has_incomplete_control:
        return "engineering"
    return "resolved"


def check_evidence_gap_root_cause(capability_id: str, claimed_category: str) -> CheckResult:
    """Root-cause classification cross-check: does an answer's claimed
    category for a capability's evidence gap (governance/engineering/
    resolved/excluded) match what the graph's own Policy/Standard/Control
    structure actually shows? Targets EM-M4: the failing transcript's
    16/10 split framed the breakdown at a different sub-grouping than
    golden's "Clinical-draft 10 vs incident-v2 10" -- this check grounds
    the underlying claim a governance/engineering split depends on, one
    named capability at a time, the same way check_entity_type_match
    grounds a counting-unit claim. See classify_evidence_gap_root_cause's
    docstring for the four-category definition and how it was validated.
    """
    if claimed_category not in _VALID_ROOT_CAUSE_CATEGORIES:
        raise ValueError(
            f"claimed_category {claimed_category!r} must be one of {sorted(_VALID_ROOT_CAUSE_CATEGORIES)}"
        )

    actual_category = classify_evidence_gap_root_cause(capability_id)
    if claimed_category != actual_category:
        return CheckResult(
            check_name="evidence_gap_root_cause",
            flagged=True,
            reason=(
                f"claimed '{capability_id}' is a '{claimed_category}' evidence gap, but its "
                f"Policy/Standard/Control structure shows '{actual_category}'"
            ),
            evidence={"capability_id": capability_id, "claimed_category": claimed_category, "actual_category": actual_category},
        )
    return CheckResult(
        check_name="evidence_gap_root_cause",
        flagged=False,
        reason=f"claimed category '{claimed_category}' matches the independently-derived structure",
        evidence={"capability_id": capability_id, "claimed_category": claimed_category, "actual_category": actual_category},
    )
