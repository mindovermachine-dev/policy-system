"""Regulatory Change Monitor: keeps tracked regulatory instruments current with CELLAR.

Re-exports the two manually invoked entry points. Importing anything else from
the submodules is for tests and internal wiring only.

Entry points:
    poll_for_amendments: Detect, for each tracked instrument, whether CELLAR
        publishes a newer consolidated version. Read-only against the graph.
    trigger_reingestion: Re-enter the full re-ingestion cycle for an amended
        instrument (Ingestion, Domain Mapper, Company Merge) and record the
        `SUPERSEDED_BY` succession last, once the new version is mapped and
        merged, so a failed run leaves the prior version active.
"""

from __future__ import annotations

from ps_service.change_monitor.poll import poll_for_amendments
from ps_service.change_monitor.trigger import trigger_reingestion

__all__ = ["poll_for_amendments", "trigger_reingestion"]
