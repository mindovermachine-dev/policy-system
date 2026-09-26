"""Parse a curated instrument's `manifest.json`, fetched from the curated-content source.

Mirrors `ps_cli.catalog_repo`'s own `_parse_manifest` validation shape
(PLAN.md §2) field-for-field -- the only difference is that this parses raw
HTTP response bytes (not an already-`json.loads`-ed local-file payload) and
raises `CuratedSourceFetchError` naming the source URL (AC-BI-006) instead of
`PsCliError` on any shape violation.
"""

from __future__ import annotations

import json
from typing import cast

from ps_service.curated_source.errors import CuratedSourceFetchError
from ps_service.export.models import InstrumentManifest

_EXTERNAL = "external"
_INTERNAL = "internal"


def _require_str(body: dict[str, object], field: str, *, url: str) -> str:
    """Return `body[field]` if it is a string; raise `CuratedSourceFetchError` otherwise."""
    value = body.get(field)
    if not isinstance(value, str):
        message = (
            f"malformed manifest.json from curated source {url!r}: "
            f"field {field!r} is missing or not a string"
        )
        raise CuratedSourceFetchError(message)
    return value


def _require_optional_str(body: dict[str, object], field: str, *, url: str) -> str | None:
    """Return `body[field]` if it is `None`/a string; raise `CuratedSourceFetchError` otherwise."""
    value = body.get(field)
    if value is not None and not isinstance(value, str):
        message = (
            f"malformed manifest.json from curated source {url!r}: "
            f"field {field!r} must be a string or null"
        )
        raise CuratedSourceFetchError(message)
    return value


def parse_manifest_json(raw_bytes: bytes, *, url: str) -> InstrumentManifest:
    """Parse `manifest.json`'s raw response bytes into an `InstrumentManifest` (AC-BI-004).

    Args:
        raw_bytes: The raw response body fetched from `url`.
        url: The exact URL `raw_bytes` was fetched from -- named in every
            error message (AC-BI-006).

    Returns:
        The parsed `InstrumentManifest`.

    Raises:
        CuratedSourceFetchError: `raw_bytes` is not valid UTF-8 JSON, is not
            a JSON object, a required field is missing/mistyped, or
            `source_type` is neither `"external"` nor `"internal"`.
    """
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        message = f"malformed manifest.json from curated source {url!r}: not valid JSON ({exc})"
        raise CuratedSourceFetchError(message) from exc
    if not isinstance(payload, dict):
        message = f"malformed manifest.json from curated source {url!r}: expected a JSON object"
        raise CuratedSourceFetchError(message)
    body = cast("dict[str, object]", payload)
    source_type = _require_str(body, "source_type", url=url)
    if source_type not in (_EXTERNAL, _INTERNAL):
        message = (
            f"malformed manifest.json from curated source {url!r}: "
            f"source_type must be 'external' or 'internal', got {source_type!r}"
        )
        raise CuratedSourceFetchError(message)
    return InstrumentManifest(
        instrument_id=_require_str(body, "instrument_id", url=url),
        celex=_require_optional_str(body, "celex", url=url),
        title=_require_str(body, "title", url=url),
        short_name=_require_str(body, "short_name", url=url),
        version=_require_str(body, "version", url=url),
        source_type=source_type,
        jurisdiction=_require_optional_str(body, "jurisdiction", url=url),
        schema_version=_require_str(body, "schema_version", url=url),
        exported_at=_require_str(body, "exported_at", url=url),
        baseline_sha256=_require_str(body, "baseline_sha256", url=url),
        native_sha256=_require_str(body, "native_sha256", url=url),
    )
