"""Runtime, on-demand fetch of one curated instrument's artifact over HTTP.

`fetch_artifact` GETs `{base_url}/{instrument_id}/manifest.json`,
`{base_url}/{instrument_id}/baseline.json` and `{base_url}/{instrument_id}/native.json`, and
parses the manifest with `manifest_parser.parse_manifest_json`. It reads over HTTP what
`ps_cli.catalog_repo.read_artifact` reads from local disk. The graph blobs are returned as
raw, unparsed bytes; verification (checksum and schema version) happens later, inside
`ps_service.restore.restore_instrument`, never here.
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

    Mirrors `ps_service.restore.models.RestoreArtifact` field for field but is a distinct type,
    because this component's responsibility ends at fetched, unverified bytes. The API boundary
    converts it into a `RestoreArtifact` immediately before calling `restore_instrument`, which
    owns all verification.

    Attributes:
        manifest: The parsed `manifest.json` of the instrument.
        baseline_blob: Raw bytes of `baseline.json`; not parsed or checksum-verified here.
        native_blob: Raw bytes of `native.json`; not parsed or checksum-verified here.
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
    """Fetch one curated instrument's manifest and both graph blobs.

    The manifest is fetched and parsed first, then the baseline and native blobs; the first
    failure stops the fetch. There is never a silent fallback to stale data.

    Args:
        base_url: Base URL of the configured curated-content source; the instrument lives under
            `{base_url}/{instrument_id}/`. Normally has no trailing slash.
        instrument_id: Id of the curated instrument; names the subdirectory to fetch from.
        transport: HTTP transport; `None` uses the default of `http_fetch.fetch_bytes` (real
            network access). Injectable so tests never reach the network.
        emitter: Log emitter; `None` uses the process-wide default emitter.

    Returns:
        A `FetchedArtifact` with the parsed manifest and both raw, unverified blobs.

    Raises:
        CuratedSourceFetchError: The source is unreachable, or any of the three files is missing
            or malformed.

    Side Effects:
        Up to three HTTP GETs and one structured log entry (component `curated_source`, action
        `fetch_artifact`, entity id `instrument_id`) with outcome `success` or `failed`.
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

    The dependency-injection seam that `run_restoration_from_catalog_source`
    (`api.restore_orchestration`) calls through, so tests can substitute a fake fetcher.
    """

    def __call__(self, base_url: str, instrument_id: str) -> FetchedArtifact:
        """Fetch one curated instrument's manifest and both blobs.

        Args:
            base_url: Base URL of the curated-content source.
            instrument_id: Id of the curated instrument.

        Returns:
            The fetched artifact; failures raise `CuratedSourceFetchError`.
        """
        ...
