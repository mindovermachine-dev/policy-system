"""Regulatory Change Monitor: package front door (PLAN_REVIEWED.md §2).

Re-exports the two manually-invoked entry points: `poll_for_amendments`
(detect newer consolidated versions in CELLAR, AC-002/003/009) and
`trigger_reingestion` (the UC-4 re-entry point that runs the full re-ingestion
cycle for an amended instrument -- Ingestion, Domain Mapper, Company Merge --
and records its `SUPERSEDED_BY` succession last, once the new version is mapped
and merged, AC-004..009).
"""

from __future__ import annotations

from ps_service.change_monitor.poll import poll_for_amendments
from ps_service.change_monitor.trigger import trigger_reingestion

__all__ = ["poll_for_amendments", "trigger_reingestion"]
