"""Runtime, on-demand fetch of one curated instrument's artifact (AC-BI-004).

`fetch_artifact` GETs `{base_url}/{instrument_id}/manifest.json`,
`{base_url}/{instrument_id}/baseline.json`, and
`{base_url}/{instrument_id}/native.json` (D-DEFAULT-URL's layout) and parses
the manifest via `manifest_parser.parse_manifest_json` -- mirrors
`ps_cli.catalog_repo.read_artifact`'s shape (PLAN.md §2) over HTTP instead of
local disk. Blobs are returned as raw, unparsed bytes -- exactly like
`read_artifact`'s own contract -- so verification (checksum, D9;
schema_version, D10) happens later, inside `ps_service.restore.
restore_instrument`, never here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.curated_source.http_fetch import CuratedSourceTransport, fetch_bytes
from ps_service.curated_source.manifest_parser import parse_manifest_json
from ps_service.logging.facade import emit_log_entry

if TYPE_CHECKING:
    from ps_service.export.models import InstrumentManifest
    from ps_service.logging import LogEmitter

_MANIFEST_FILENAME = "manifest.json"
_BASELINE_FILENAME = "baseline.json"
_NATIVE_FILENAME = "native.json"
_COMPONENT = "curated_source"
_ACTION = "fetch_artifact"


@dataclass(frozen=True, slots=True)
class FetchedArtifact:
    """One curated instrument's artifact, fetched from the configured curated-content source.

    Field-for-field mirror of `ps_service.restore.models.RestoreArtifact` --
    a distinct type (not a reuse of that one), since this component's own
    responsibility ends at "fetched, unverified bytes" (Single
    Responsibility). The API-boundary orchestration converts this into a
    `RestoreArtifact` immediately before handing it to `restore_instrument`,
    which owns all verification (D9/D10).
    """

    manifest: InstrumentManifest
    baseline_blob: bytes
    native_blob: bytes


def _fetch(url: str, transport: CuratedSourceTransport | None) -> bytes:
    """Call `http_fetch.fetch_bytes`, forwarding `transport` only when given."""
    return fetch_bytes(url, transport=transport) if transport is not None else fetch_bytes(url)


def fetch_artifact(
    base_url: str,
    instrument_id: str,
    *,
    transport: CuratedSourceTransport | None = None,
    emitter: LogEmitter | None = None,
) -> FetchedArtifact:
    """Fetch one curated instrument's manifest and both blobs (AC-BI-004).

    Emits one structured log entry on completion -- `outcome="success"` or
    `outcome="failed"` -- mirroring `catalog_client.fetch_catalog`'s own
    `emit_log_entry` usage (matched here by the `curated_source`/
    `fetch_artifact` component/action pair), with `instrument_id` as the
    log entry's `entity_id`.

    Args:
        base_url: The configured curated-content source's base URL
            (D-DEFAULT-URL layout: `{base_url}/{instrument_id}/...`).
        instrument_id: The curated instrument id, naming the
            `{base_url}/{instrument_id}/` subdirectory to fetch from.
        transport: The HTTP transport to use -- `None` (the default) keeps
            `http_fetch.fetch_bytes`'s own default (the real
            `urllib.request.urlopen`); call-site injectable so tests never
            reach real network.
        emitter: The `LogEmitter` to emit through -- `None` (the default)
            falls back to the process-wide default emitter, mirroring every
            other `emitter: LogEmitter | None = None` call site in this
            codebase.

    Returns:
        A `FetchedArtifact` carrying the parsed manifest and both raw,
        unverified blob bytes.

    Raises:
        CuratedSourceFetchError: The source is unreachable, or any of the
            three fetched files is missing/malformed (AC-BI-006) -- never a
            silent fallback to stale data.
    """
    manifest_url = f"{base_url}/{instrument_id}/{_MANIFEST_FILENAME}"
    baseline_url = f"{base_url}/{instrument_id}/{_BASELINE_FILENAME}"
    native_url = f"{base_url}/{instrument_id}/{_NATIVE_FILENAME}"
    try:
        manifest_bytes = _fetch(manifest_url, transport)
        manifest = parse_manifest_json(manifest_bytes, url=manifest_url)
        baseline_blob = _fetch(baseline_url, transport)
        native_blob = _fetch(native_url, transport)
    except CuratedSourceFetchError as exc:
        emit_log_entry(
            component=_COMPONENT,
            action=_ACTION,
            entity_id=instrument_id,
            outcome="failed",
            extra={"source": base_url, "error": str(exc)},
            emitter=emitter,
        )
        raise
    emit_log_entry(
        component=_COMPONENT,
        action=_ACTION,
        entity_id=instrument_id,
        outcome="success",
        extra={"source": base_url},
        emitter=emitter,
    )
    return FetchedArtifact(manifest=manifest, baseline_blob=baseline_blob, native_blob=native_blob)


class FetchArtifactCall(Protocol):
    """Call shape of :func:`fetch_artifact`.

    The DI seam `run_restoration_from_catalog_source`
    (`api.restore_orchestration`) calls through.
    """

    def __call__(self, base_url: str, instrument_id: str) -> FetchedArtifact:
        """Fetch one curated instrument's manifest and both blobs."""
        ...
