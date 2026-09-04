"""ps_service.export -- package front door.

Re-exports `InstrumentManifest` (`ps_service.export.models`), matching the
`ps_service.domain_mapper` package front door's own re-export convention.
"""

from __future__ import annotations

from ps_service.export.models import InstrumentManifest

__all__ = [
    "InstrumentManifest",
]
