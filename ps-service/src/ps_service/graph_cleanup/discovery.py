"""Pure candidate grouping for `ps_service.graph_cleanup` (issue #190).

Discovery is advisory: a human Compliance Officer decides every merge, so a
wrong guess costs review time, never data. No LLM or embedding-model call is
ever made here; the only reuse of Company Merge is the pure `cosine_similarity`
over embeddings already cached on the Capability nodes.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.company_merge.similarity import cosine_similarity
from ps_service.graph_cleanup.models import (
    CapabilityCandidateGroup,
    CapabilityCandidateMember,
    ObligationCandidateGroup,
    ObligationCandidateMember,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ps_service.graph_cleanup.models import CapabilityRecord, MergeCase, ObligationRecord

__all__ = [
    "DEFAULT_CAPABILITY_MIN_SIMILARITY",
    "OBLIGATION_NEAR_TEXT_MIN_JACCARD",
    "classify_case",
    "group_capabilities",
    "group_duplicate_obligations",
    "normalise_name",
    "resolve_min_similarity",
]

# Advisory default (CHANGES.md E5, recorded in the slice-7 commit): high enough that a
# group is nearly always a true duplicate, used only when neither the caller nor
# `company_merge_similarity_threshold` supplies a value. A wrong guess costs review time.
DEFAULT_CAPABILITY_MIN_SIMILARITY = 0.90

# Advisory: token-set Jaccard of normalised Obligation text at or above which two Obligations
# of one Role are listed as near-identical duplicates. Cheap and explainable; a human decides.
OBLIGATION_NEAR_TEXT_MIN_JACCARD = 0.8

_NON_ALNUM = re.compile(r"[\W_]+")


def normalise_name(name: str) -> str:
    """Casefold `name` and collapse every run of non-alphanumerics to one space."""
    return _NON_ALNUM.sub(" ", name.casefold()).strip()


def resolve_min_similarity(requested: float | None, configured: float | None) -> float:
    """Pick the threshold: explicit request, else the Company Merge value, else the default."""
    if requested is not None:
        return requested
    if configured is not None:
        return configured
    return DEFAULT_CAPABILITY_MIN_SIMILARITY


def classify_case(members: Sequence[CapabilityRecord]) -> tuple[MergeCase, bool]:
    """Return a group's `(merge_case, policies_distinct)` (CHANGES D-A2).

    Case 1: no member is governed; case 2: exactly one is; case 3: two or more are.
    `policies_distinct` is true when the governed members name more than one Policy,
    which is what makes a case-3 merge blocked (release governance first).
    """
    governed = {m.governing_policy.id for m in members if m.governing_policy is not None}
    count = sum(1 for m in members if m.governing_policy is not None)
    case: MergeCase = 1 if count == 0 else 2 if count == 1 else 3
    return case, len(governed) > 1


def _find(parent: list[int], index: int) -> int:
    while parent[index] != index:
        parent[index] = parent[parent[index]]
        index = parent[index]
    return index


def _union(parent: list[int], a: int, b: int) -> None:
    root_a, root_b = _find(parent, a), _find(parent, b)
    if root_a != root_b:
        parent[max(root_a, root_b)] = min(root_a, root_b)


def _similar(a: CapabilityRecord, b: CapabilityRecord, threshold: float) -> bool:
    if a.embedding is None or b.embedding is None:
        return False
    try:
        return cosine_similarity(a.embedding, b.embedding) >= threshold
    except CompanyMergeValidationError:
        # Mismatched dimensions (an embedding model change) or a zero vector: this pair is
        # unscorable, which must never abort the whole sweep. Name basis still applies.
        return False


def group_capabilities(
    capabilities: Sequence[CapabilityRecord], *, min_similarity: float
) -> tuple[CapabilityCandidateGroup, ...]:
    """Group Capabilities linked by equal names or cached-embedding cosine >= `min_similarity`.

    Groups are connected components (two or more members). `basis` is `"name"` when every
    member shares one normalised name, else `"embedding"`. A Capability with no cached
    embedding can only be linked by name. Pairwise cosine is O(n^2) over the active
    Capabilities that carry an embedding; pairs already in one component are skipped.
    Members are ordered by id and groups by their first member's id (deterministic).
    """
    ordered = sorted(capabilities, key=lambda c: c.id)
    parent = list(range(len(ordered)))
    first_by_name: dict[str, int] = {}
    for index, capability in enumerate(ordered):
        key = normalise_name(capability.name)
        if key in first_by_name:
            _union(parent, first_by_name[key], index)
        else:
            first_by_name[key] = index
    for i, left in enumerate(ordered):
        if left.embedding is None:
            continue
        for j in range(i + 1, len(ordered)):
            if _find(parent, i) != _find(parent, j) and _similar(left, ordered[j], min_similarity):
                _union(parent, i, j)

    components: dict[int, list[CapabilityRecord]] = defaultdict(list)
    for index, capability in enumerate(ordered):
        components[_find(parent, index)].append(capability)
    groups = [_build_group(members) for members in components.values() if len(members) > 1]
    return tuple(sorted(groups, key=lambda g: g.members[0].id))


def _build_group(members: Sequence[CapabilityRecord]) -> CapabilityCandidateGroup:
    case, distinct = classify_case(members)
    return CapabilityCandidateGroup(
        basis="name" if len({normalise_name(c.name) for c in members}) == 1 else "embedding",
        merge_case=case,
        policies_distinct=distinct,
        members=tuple(
            CapabilityCandidateMember(
                id=c.id,
                name=c.name,
                obligation_count=c.obligation_count,
                governing_policy=c.governing_policy,
            )
            for c in members
        ),
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def group_duplicate_obligations(
    obligations: Sequence[ObligationRecord],
) -> tuple[ObligationCandidateGroup, ...]:
    """Group Obligations of ONE Role whose normalised text is identical or near-identical.

    Never links across Roles: an Obligation is a weak entity of its Role and the same
    wording under two Roles is two distinct duties (DC Obligation). Near-identical means
    token-set Jaccard >= `OBLIGATION_NEAR_TEXT_MIN_JACCARD`. `basis` is `"identical_text"`
    when every member shares one normalised text, else `"near_text"`. Members are ordered
    by id and groups by (role id, first member id).
    """
    by_role: dict[str, list[ObligationRecord]] = defaultdict(list)
    for obligation in sorted(obligations, key=lambda o: o.id):
        by_role[obligation.role_id].append(obligation)
    groups: list[ObligationCandidateGroup] = []
    for role_id in sorted(by_role):
        members = by_role[role_id]
        normalised = [normalise_name(o.text) for o in members]
        tokens = [frozenset(text.split()) for text in normalised]
        parent = list(range(len(members)))
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if _find(parent, i) == _find(parent, j):
                    continue
                if (
                    normalised[i] == normalised[j]
                    or _jaccard(tokens[i], tokens[j]) >= OBLIGATION_NEAR_TEXT_MIN_JACCARD
                ):
                    _union(parent, i, j)
        components: dict[int, list[int]] = defaultdict(list)
        for index in range(len(members)):
            components[_find(parent, index)].append(index)
        groups.extend(
            ObligationCandidateGroup(
                role_id=role_id,
                role_name=members[indexes[0]].role_name,
                basis=(
                    "identical_text" if len({normalised[i] for i in indexes}) == 1 else "near_text"
                ),
                members=tuple(
                    ObligationCandidateMember(
                        id=members[i].id,
                        text=members[i].text,
                        requirements=tuple(
                            sorted(
                                members[i].requirements,
                                key=lambda r: (r.source_ref or "", r.requirement_id),
                            )
                        ),
                    )
                    for i in indexes
                ),
            )
            for indexes in components.values()
            if len(indexes) > 1
        )
    return tuple(groups)
