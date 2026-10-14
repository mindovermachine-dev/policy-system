"""ps_service.company_merge.governance_import -- the shared draft-governance import pass.

A Policy/Standard/Control tree that arrives from outside the customer's governance -- a restored
curated instrument, or a live internal-document ingest -- is written `draft`, owned by the
importing caller, each node under its own content-hashed id
(`graph_writer.persist_governance_drafts`), and never converged onto an existing canonical Policy.
Both entry points call `import_draft_governance`, so the two paths cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ps_service.company_merge.errors import GovernanceOwnerRequiredError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ps_service.company_merge.falkordb_client import GraphHandle
    from ps_service.company_merge.models import (
        BaselineGraph,
        BaselineNode,
        GovernanceDraftCounts,
    )


class DraftGovernanceWriter(Protocol):
    """The write step of the pass: persists a Policy/Standard/Control tree as `draft`.

    Company Merge passes `graph_writer.persist_governance_drafts` with its gateway write target
    bound (a Graph Write Gateway group); Restore passes its own primitive writer. `graph` is the
    handle the writer reads through.
    """

    def __call__(
        self,
        graph: GraphHandle,
        policy_nodes: tuple[BaselineNode, ...],
        standard_nodes: tuple[BaselineNode, ...],
        control_nodes: tuple[BaselineNode, ...],
        /,
        *,
        owner: tuple[str, str],
        policy_embeddings: Mapping[str, tuple[float, ...]] | None,
    ) -> GovernanceDraftCounts:
        """Persist the tree as `draft` and return what was written."""
        ...


@dataclass(frozen=True, slots=True)
class GovernanceImport:
    """The caller-supplied inputs the draft-governance pass needs.

    Bundled so callers keep a bounded parameter count.

    Attributes:
        owner: The importing caller's verified `(sub, iss)` pair, the owner of every imported
            draft Policy. `None` when no verified identity exists, in which case governance
            content is refused.
        policy_embeddings: Artifact-supplied Policy vectors by Policy id, stored on the draft.
            Empty on the live ingest path.
    """

    owner: tuple[str, str] | None = None
    policy_embeddings: dict[str, tuple[float, ...]] = field(default_factory=dict)


def import_draft_governance(
    baseline: BaselineGraph,
    *,
    single_tenant_graph: GraphHandle,
    governance: GovernanceImport,
    canonical_id_by_incoming_id: dict[str, str],
    write_drafts: DraftGovernanceWriter,
) -> GovernanceDraftCounts | None:
    """Import `baseline`'s Policy/Standard/Control tree into `single_tenant_graph` as `draft`.

    A structural no-op (zero writes, returns `None`) when `baseline` has no
    governance content -- an external-sourced baseline. Otherwise
    `governance.owner` must be set.

    Each Policy maps onto itself in `canonical_id_by_incoming_id` (mutated in
    place): `persist_rewired_edges` requires an entry for every Policy endpoint
    of `GOVERNED_BY`/`SUPPORTED_BY`/`OWNS`, and here canonical == incoming.

    `write_drafts` is the persistence step and has no default: the caller decides whether the tree
    goes through the gateway (live merge) or into a staged snapshot (restore).

    Args:
        baseline: The baseline whose Policy/Standard/Control nodes are imported.
        single_tenant_graph: Handle passed to `write_drafts` as the graph it reads through.
        governance: The owner and artifact-supplied Policy embeddings for the import.
        canonical_id_by_incoming_id: Incoming-to-canonical id mapping; mutated in place with one
            self-mapping per imported Policy.
        write_drafts: The persistence step that writes the tree as `draft`.

    Returns:
        The written counts, or `None` when there was nothing to import.

    Raises:
        GovernanceOwnerRequiredError: Governance content is present and
            `governance.owner` is `None`; raised before any write.

    Side Effects:
        Calls `write_drafts` once and adds the Policy self-mappings to
        `canonical_id_by_incoming_id`.
    """
    if not (baseline.policy_nodes or baseline.standard_nodes or baseline.control_nodes):
        return None
    if governance.owner is None:
        raise GovernanceOwnerRequiredError(
            "an imported policy tree needs an owner; none was supplied"
        )
    counts = write_drafts(
        single_tenant_graph,
        baseline.policy_nodes,
        baseline.standard_nodes,
        baseline.control_nodes,
        owner=governance.owner,
        policy_embeddings=governance.policy_embeddings,
    )
    canonical_id_by_incoming_id.update({policy.id: policy.id for policy in baseline.policy_nodes})
    return counts


def governance_count_fields(counts: GovernanceDraftCounts | None) -> dict[str, int]:
    """Return the four audit/summary count fields for `counts`.

    Args:
        counts: The written counts, or `None` when no governance was imported.

    Returns:
        A dict with `governance_policies`, `governance_standards`, `governance_controls` and
        `governance_status_overridden`; every value is 0 when `counts` is `None`.
    """
    return {
        "governance_policies": counts.policies if counts else 0,
        "governance_standards": counts.standards if counts else 0,
        "governance_controls": counts.controls if counts else 0,
        "governance_status_overridden": counts.status_overridden if counts else 0,
    }
