"""ps_service.company_merge -- package front door.

Re-exports `merge_baseline_graph` (`ps_service.company_merge.merge`), the
one public action this component exposes (PLAN_REVIEWED.md §1's
file-layout intent, §7). `DedupeCanonicalNodes` is deliberately not
re-exported here -- it only ever runs as part of `merge_baseline_graph`,
never as its own independently-invocable entry point (PLAN_REVIEWED.md
§0.1/§7).
"""

from __future__ import annotations

from ps_service.company_merge.merge import merge_baseline_graph

__all__ = ["merge_baseline_graph"]
