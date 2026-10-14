"""ps_service.company_merge -- package front door.

Re-exports `merge_baseline_graph` (`ps_service.company_merge.merge`), the one public action this
component exposes: it folds a regulation's baseline graph into the single-tenant graph. Dedup
(`dedupe_canonical_nodes`) is deliberately not re-exported: it only runs as part of
`merge_baseline_graph`, never as an independently invocable entry point.
"""

from __future__ import annotations

from ps_service.company_merge.merge import merge_baseline_graph

__all__ = ["merge_baseline_graph"]
