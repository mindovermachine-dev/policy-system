"""Named, justified exceptions to "every graph name is in the domain schema".

The pin tests (internal_seed vocabulary, restore allow-lists, company_merge
read set, Cypher scan) compare a hand-written vocabulary copy with
`DOMAIN_SCHEMA`. A few names legitimately appear on only one side; each is
listed here with the reason, so a new exception is a conscious, reviewed edit
rather than a silent widening. Imported by tests only: the restore allow-lists
must stay independent of this package (AC-BI-014).
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
        "MERGED_INTO": "minted by graph cleanup / company merge as a tombstone pointer",
    }
)

OPERATIONAL_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "MergedObligation": "graph-cleanup marker node, not a domain node",
        "PendingReview": "near-miss review store, not a domain node",
        "ReingestProgress": (
            "change-monitor re-ingest stage marker in the native graph; "
            "deleted on success, not a domain node"
        ),
    }
)

CELLAR_ELI_NATIVE_LABELS: Mapping[str, str] = MappingProxyType(
    dict.fromkeys(
        ("TITLE", "CHAPTER", "SECTION", "ARTICLE", "PARAGRAPH", "ANNEX", "RECITAL"),
        "native graph vocabulary of the Cellar/ELI adapter",
    )
)


def find_unpinned(
    names: Iterable[str], schema_names: Iterable[str], exceptions: Iterable[str]
) -> tuple[str, ...]:
    """Return the sorted names that are neither schema names nor named exceptions."""
    known = set(schema_names) | set(exceptions)
    return tuple(sorted({name for name in names if name not in known}))
