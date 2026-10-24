"""Named, justified exceptions to "every graph name is in the domain schema".

The pin tests (internal_seed vocabulary, restore allow-lists, company_merge read set, Cypher
scan) compare a hand-written vocabulary copy with `DOMAIN_SCHEMA`. A few names legitimately
appear on only one side; each is listed here with the reason, so a new exception is a conscious,
reviewed edit rather than a silent widening. The exception sets are imported by tests and, at
runtime, by the Graph Write Gateway (`ps_service.graph_gateway.label_allow_list`), which builds
its label allow-list from `DOMAIN_SCHEMA` plus these named sets.
The restore allow-lists must stay independent of this package.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

SYSTEM_MINTED_EDGE_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "SUPERSEDED_BY": "minted by change monitor / policy lifecycle, never in an intake document",
        "TRANSPOSES": "minted by the domain mapper from legislation, never submitted",
    }
)

MERGE_MARKER_LABELS: Mapping[str, str] = MappingProxyType(
    dict.fromkeys(
        ("MergedCapability", "MergedObligation", "MergedRole"),
        "graph-cleanup marker node, not a domain node",
    )
)
"""One `{id, merged_into}` marker per absorbed id and kind. A marker belongs to the deployment's
own graph: no export artifact carries one and no restore allow-list admits one."""

RESOLUTION_MARKER_LABELS: Mapping[str, str] = MappingProxyType(
    {"ResolvedCapability": "company-merge semantic-resolution marker node, not a domain node"}
)
"""One `{id, resolves_to}` marker per incoming Capability id that Company Merge resolved onto an
existing Capability by similarity. It records a decision, not an absorbed node: there is no audit
snapshot behind it and nothing to unmerge. Like a merge marker it belongs to the deployment's own
graph: no export artifact carries one and no restore allow-list admits one."""

REDIRECT_MARKER_LABELS: Mapping[str, str] = MappingProxyType(
    {**MERGE_MARKER_LABELS, **RESOLUTION_MARKER_LABELS}
)
"""Every marker label Company Merge follows from an incoming id to its terminal survivor."""

OPERATIONAL_LABELS: Mapping[str, str] = MappingProxyType(
    {
        **REDIRECT_MARKER_LABELS,
        "PendingReview": "near-miss review store, not a domain node",
        "ReingestProgress": (
            "change-monitor re-ingest stage marker in the native graph; "
            "deleted on success, not a domain node"
        ),
    }
)

REPLAY_SENTINEL_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "GraphReplayState": (
            "graph-gateway replay-progress sentinel (#207): bookkeeping node present only while a "
            "replay runs, excluded from the canonical digest, not a domain node"
        ),
    }
)
"""Labels written only by the gateway's own replay machinery. Deliberately not part of the
gateway's label allow-list: a writer forging or deleting the sentinel could fake replay progress
or clear a failed gate. Only the Cypher vocabulary scan admits them."""

CELLAR_ELI_NATIVE_LABELS: Mapping[str, str] = MappingProxyType(
    dict.fromkeys(
        ("TITLE", "CHAPTER", "SECTION", "ARTICLE", "PARAGRAPH", "ANNEX", "RECITAL"),
        "native graph vocabulary of the Cellar/ELI adapter",
    )
)


def find_unpinned(
    names: Iterable[str], schema_names: Iterable[str], exceptions: Iterable[str]
) -> tuple[str, ...]:
    """Return the sorted names that are neither schema names nor named exceptions.

    Args:
        names: The hand-written vocabulary to check.
        schema_names: The names `DOMAIN_SCHEMA` defines.
        exceptions: The named exceptions accepted in addition (for example a mapping's keys).

    Returns:
        The unknown names, deduplicated and sorted; empty when the vocabulary is fully pinned.
    """
    known = set(schema_names) | set(exceptions)
    return tuple(sorted({name for name in names if name not in known}))
