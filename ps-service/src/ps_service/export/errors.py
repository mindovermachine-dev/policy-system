"""Domain-specific exception types for `ps_service.export`.

One exception type per distinct failure boundary this component owns, never
a generic `Exception`/`ValueError` (L1 Error Handling, L2 Error Handling).
`ExportConfigurationError` (PLAN.md's module table) is not built here --
confirmed unreferenced by any real (non-planning-doc) code as of CHANGES2.md's
redesign, so it is not speculatively added; a future dispatch adds it if/when
something actually needs it.
"""

from __future__ import annotations


class ExportSourceGraphError(Exception):
    """A source graph's shape is not exportable by `serialize.serialize_graph`.

    Raised when any node, or any edge endpoint, carries zero or more than
    one label -- every writer in this codebase mints single-labeled nodes
    (`MERGE (n:{label} {id: ...})`), so this signals a genuinely
    out-of-contract graph, never silently coerced to "pick one label".
    """
