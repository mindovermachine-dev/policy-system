"""ps_service.curated_source -- package front door.

Runtime fetch of the curated-content catalog listing and, in a later slice,
per-instrument artifacts, from a configurable HTTP(S) source (issue #125),
replacing the build-time-packaged `catalog.json` copy `ps_service.api.catalog`
previously served unconditionally. Domain path: `ps.service.curatedsource`
(`docs/architecture/ps-service-container-architecture.md`).

Re-exports `CuratedSourceConfigurationError`/`CuratedSourceFetchError`
(`ps_service.curated_source.errors`), matching the `ps_service.restore`/
`ps_service.export` package front doors' own re-export convention.

The runtime override of the source URL lives in `ps_service.runtime_config` (issue #130);
importing `ps_service.curated_source.config_key` below registers its key, so the key is
registered whenever any part of this component is used.
"""

from __future__ import annotations

import ps_service.curated_source.config_key  # noqa: F401  # pyright: ignore[reportUnusedImport] -- side-effect import: registers the catalog-source runtime-config key
from ps_service.curated_source.errors import (
    CuratedSourceConfigurationError,
    CuratedSourceFetchError,
)

__all__ = [
    "CuratedSourceConfigurationError",
    "CuratedSourceFetchError",
]
