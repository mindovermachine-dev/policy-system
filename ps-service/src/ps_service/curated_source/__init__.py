"""Runtime fetch of curated-content catalog listings and instrument artifacts.

This component reads the curated-content catalog listing and, per instrument, the artifact
(manifest plus baseline and native graph blobs) from a configurable HTTP(S) source, instead of
a copy packaged at build time. Domain path: `ps.service.curatedsource`
(`docs/architecture/ps-service-container-architecture.md`).

Re-exports `CuratedSourceConfigurationError` and `CuratedSourceFetchError`
(`ps_service.curated_source.errors`), as the `ps_service.restore` and `ps_service.export`
package front doors re-export their errors.

The runtime override of the source URL lives in `ps_service.runtime_config`. Importing
`ps_service.curated_source.config_key` below registers its key, so the key is registered
whenever any part of this component is used.
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
