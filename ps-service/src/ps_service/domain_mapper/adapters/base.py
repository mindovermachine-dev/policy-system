"""The pluggable Domain Mapping Adapter interface
(`ps_service.domain_mapper.adapters.base`) — CA doc Implementation
Guidance: "Reads the native structural graph... through a Domain Mapping
Adapter... one per regulatory source, paired 1:1 with that source's
Ingestion Adapter."
"""

from __future__ import annotations

from typing import Protocol

from ps_service.domain_mapper.falkordb_client import GraphHandle
from ps_service.domain_mapper.models import ExtractionUnit


class DomainMappingAdapter(Protocol):
    """One method, matching PLAN_REVIEWED.md §4.1. Reads a regulation's
    native structural graph (already selected via `graph` — the caller
    resolves `{short}_native`, this Protocol imposes no naming convention
    of its own) and returns the ordered sequence of extraction units.
    Paired 1:1 with an Ingestion Adapter — a Domain Mapping Adapter's
    expected input shape must track its paired Ingestion Adapter's output
    shape exactly.
    """

    def read_native_units(self, graph: GraphHandle) -> tuple[ExtractionUnit, ...]: ...
