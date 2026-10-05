"""ps_service.graph_cleanup -- Compliance Officer graph cleanup (issue #190).

Privileged, human-decided cleanup of duplicates the ingestion pipeline left in
the single-tenant compliance graph: candidate discovery, and (later slices)
merge / release-governance / unmerge. Domain path: `ps.service.graphcleanup`.

Deliberately separate from Company Merge, which stays add/merge-only
(`docs/architecture/ps-service-container-architecture.md`, Company Merge);
Company Merge only gains redirect-following for the `merged` tombstones this
component writes.
"""

from __future__ import annotations
