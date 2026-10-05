"""Obligation redirect for Company Merge (issue #190, CHANGES.md H1 / A7).

A Compliance Officer cleanup merge of two Obligations deletes the absorbed one and leaves a
`MergedObligation {id, merged_into}` marker. Obligation ids are content hashes, so a later
live merge or offline restore whose baseline regenerates the absorbed Obligation would
otherwise mint it again. Before any persist, `resolve_obligation_redirects` drops such a node
from the baseline and maps its id to the terminal survivor; the caller folds that mapping
into `canonical_id_by_incoming_id`, so the existing endpoint-agnostic `persist_rewired_edges`
unions `SATISFIED_BY` / `REQUIRES` onto the survivor and re-MERGEs the (idempotent) `HAS` edge.

Read-only; every failure is raised before the caller's first write.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from ps_service.company_merge.errors import CompanyMergeValidationError
from ps_service.logging import emit_log_entry

if TYPE_CHECKING:
    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import BaselineGraph
    from ps_service.logging import LogEmitter

__all__ = [
    "apply_obligation_redirects",
    "follow_obligation_redirect",
    "read_obligation_redirects",
    "resolve_obligation_redirects",
]

_MARKER_QUERY = "MATCH (m:MergedObligation) RETURN m.id, m.merged_into"
_EXISTING_QUERY = "MATCH (o:Obligation) WHERE o.id IN $ids RETURN o.id"


def read_obligation_redirects(single_tenant_graph: GraphHandle) -> dict[str, str]:
    """Read every `MergedObligation` marker as `{absorbed_id: merged_into}` (one hop)."""
    result = single_tenant_graph.query(_MARKER_QUERY)
    rows = cast("list[list[object]]", result.result_set)
    return {cast("str", row[0]): cast("str", row[1]) for row in rows}


def follow_obligation_redirect(obligation_id: str, redirects: dict[str, str]) -> str:
    """Follow `obligation_id`'s marker chain to its terminal survivor.

    An id with no marker is its own terminal.

    Raises:
        CompanyMergeValidationError: the chain contains a cycle.
    """
    seen: set[str] = set()
    current = obligation_id
    while current in redirects:
        if current in seen:
            message = f"MergedObligation cycle detected at obligation {current!r}"
            raise CompanyMergeValidationError(message)
        seen.add(current)
        current = redirects[current]
    return current


def apply_obligation_redirects(
    baseline: BaselineGraph, redirects: dict[str, str], existing_ids: frozenset[str]
) -> tuple[BaselineGraph, dict[str, str]]:
    """Drop absorbed Obligations from `baseline`; map each to its terminal survivor.

    `existing_ids` are the Obligation ids known to exist in the single-tenant graph. A
    baseline with no absorbed Obligation is returned unchanged with an empty mapping.

    Raises:
        CompanyMergeValidationError: a cycle, or a terminal survivor that exists neither in
            `existing_ids` nor in `baseline.obligation_nodes`.
    """
    absorbed = [node.id for node in baseline.obligation_nodes if node.id in redirects]
    if not absorbed:
        return baseline, {}
    baseline_ids = {node.id for node in baseline.obligation_nodes}
    mapping: dict[str, str] = {}
    for absorbed_id in absorbed:
        terminal = follow_obligation_redirect(absorbed_id, redirects)
        if terminal not in existing_ids and (terminal in redirects or terminal not in baseline_ids):
            message = (
                f"merged obligation {absorbed_id!r} redirects to {terminal!r}, which does not exist"
            )
            raise CompanyMergeValidationError(message)
        mapping[absorbed_id] = terminal
    kept = tuple(node for node in baseline.obligation_nodes if node.id not in mapping)
    return replace(baseline, obligation_nodes=kept), mapping


def resolve_obligation_redirects(
    single_tenant_graph: GraphHandle,
    baseline: BaselineGraph,
    *,
    emitter: LogEmitter | None = None,
) -> tuple[BaselineGraph, dict[str, str]]:
    """Read the markers and apply them to `baseline` (one extra read only when one applies).

    Emits one `redirect_merged_obligations` log entry when at least one Obligation is redirected.
    """
    redirects = read_obligation_redirects(single_tenant_graph)
    absorbed = [node.id for node in baseline.obligation_nodes if node.id in redirects]
    if not absorbed:
        return baseline, {}
    baseline_ids = {node.id for node in baseline.obligation_nodes}
    terminals = sorted(
        {
            terminal
            for absorbed_id in absorbed
            if (terminal := follow_obligation_redirect(absorbed_id, redirects)) not in baseline_ids
        }
    )
    existing: frozenset[str] = frozenset()
    if terminals:
        result = single_tenant_graph.query(_EXISTING_QUERY, {"ids": terminals})
        rows = cast("list[list[object]]", result.result_set)
        existing = frozenset(cast("str", row[0]) for row in rows)
    rewritten, mapping = apply_obligation_redirects(baseline, redirects, existing)
    emit_log_entry(
        component="company_merge",
        action="redirect_merged_obligations",
        entity_id=baseline.regulatory_instrument_id,
        outcome="succeeded",
        extra={"redirected_count": len(mapping)},
        emitter=emitter,
    )
    return rewritten, mapping
