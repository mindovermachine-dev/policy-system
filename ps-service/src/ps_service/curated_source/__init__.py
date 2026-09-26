"""ps_service.curated_source -- package front door.

Runtime fetch of the curated-content catalog listing and, in a later slice,
per-instrument artifacts, from a configurable HTTP(S) source (issue #125),
replacing the build-time-packaged `catalog.json` copy `ps_service.api.catalog`
previously served unconditionally. Domain path: `ps.service.curatedsource`
(`docs/architecture/ps-service-container-architecture.md`).

Re-exports `CuratedSourceConfigurationError`/`CuratedSourceFetchError`
(`ps_service.curated_source.errors`), matching the `ps_service.restore`/
`ps_service.export` package front doors' own re-export convention.
"""

from __future__ import annotations

from ps_service.curated_source.errors import (
    CuratedSourceConfigurationError,
    CuratedSourceFetchError,
)

__all__ = [
    "CuratedSourceConfigurationError",
    "CuratedSourceFetchError",
]
