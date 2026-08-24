"""The pluggable Ingestion Adapter interface (`ps_service.ingestion.adapters.
base`) — CA doc Implementation Guidance: "Source-specific fetch/persist
logic lives behind an Ingestion Adapter interface... one concrete adapter
per regulatory source."
"""

from __future__ import annotations

from typing import Protocol

from ps_service.ingestion.models import FetchedRegulationStructure


class IngestionAdapter(Protocol):
    """One method, matching the CA doc's single `FetchRegulationStructure`
    action. `identifier` is a CELEX number for the Cellar/ELI adapter
    (CELEX-only per the user's decision — see PLAN_REVIEWED.md §1.2/§9,
    Open Question 2) — the Protocol itself imposes no format, so a future
    adapter (e.g. SOX/HIPAA) can define its own identifier shape without
    touching this interface.
    """

    def fetch_regulation_structure(self, identifier: str) -> FetchedRegulationStructure: ...
