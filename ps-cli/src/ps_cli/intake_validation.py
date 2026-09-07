"""Local JSON Schema validation for `ps-cli internal ingest`'s submitted file (D3/D7).

`validate_local_seed_file` is called by `handlers.handle_internal_ingest` before
any network call reaches PS Service (AC-BI-019/B4). `ps-cli` and `ps-service`
share a filesystem in every environment this issue targets (D3's deployment
reconciliation) -- `ps-cli` validates the same file, at the same relative path,
that `ps-service` will independently resolve and validate itself server-side.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import TYPE_CHECKING, Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import (
    ValidationError,
    best_match,  # pyright: ignore[reportUnknownVariableType]  # jsonschema: best_match's return type is inferred Unknown by its own stub
)

from ps_cli.errors import PsCliError

if TYPE_CHECKING:
    from pathlib import Path

_SCHEMA_PACKAGE = "ps_cli.schemas"
_SCHEMA_RESOURCE_NAME = "internal_regulation_intake_v1.schema.json"


@lru_cache(maxsize=1)
def _load_validator() -> Draft202012Validator:
    """Load and compile `ps-cli`'s packaged copy of the schema; cached for the process lifetime.

    Reads the packaged copy (D7: `ps_cli/schemas/internal_regulation_intake_v1
    .schema.json`, byte-identical to `docs/artifacts/schemas/internal-regulation-
    intake.v1.schema.json`) via `importlib.resources`, never a relative
    filesystem path, so resolution is identical whether run from source or an
    installed wheel.
    """
    resource = resources.files(_SCHEMA_PACKAGE).joinpath(_SCHEMA_RESOURCE_NAME)
    schema = json.loads(resource.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _read_seed_document(path: Path) -> dict[str, object]:
    """Read and JSON-parse `path` locally, raising `PsCliError` for anything not readable.

    Raises `PsCliError` if `path` does not exist, is not a regular file, or is
    not valid JSON, or if the parsed JSON is not itself an object -- always
    naming the specific problem, never a generic parse failure (AC-BI-019).
    """
    if not path.is_file():
        raise PsCliError(
            msg=f"fixture file not found: {path}",
            hint="check the path is relative to your configured fixtures_root",
        )
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PsCliError(msg=f"fixture file at {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        msg = (
            f"fixture file at {path} must contain a JSON object at its root, "
            f"got {type(parsed).__name__}"
        )
        raise PsCliError(msg=msg)
    return cast("dict[str, object]", parsed)


def _describe_first_violation(
    validator: Draft202012Validator, document: dict[str, object]
) -> str | None:
    """Return "{path}: {message}" for `document`'s single most relevant schema violation, or `None`.

    Isolates every interaction with `jsonschema`'s own untyped/partially-typed
    stub to this one function (L2 Types Handling: untyped third-party
    libraries are handled by a commented `# pyright: ignore[...]` at their
    single boundary module, never a global downgrade) -- mirrors
    `ps_service.ingestion.adapters.internal_seed.schema._describe_first_violation`
    exactly (vendored independently per the workspace's "no shared internal
    package" rule, not imported).
    """
    errors = validator.iter_errors(  # pyright: ignore[reportUnknownMemberType]  # jsonschema: iter_errors' overloaded return type is partially unknown in the stub
        cast("Any", document)  # pyright: ignore[reportArgumentType]  # jsonschema: stub's recursive _JsonParameter alias cannot express "arbitrary parsed JSON"
    )
    error = cast("ValidationError | None", best_match(errors))
    if error is None:
        return None
    path_segments = cast("list[object]", error.path)
    location = "/".join(str(segment) for segment in path_segments) or "<document root>"
    return f"{location}: {error.message}"


def validate_local_seed_file(path: Path) -> None:
    """Validate the local file at `path` against the packaged intake-format JSON Schema.

    Raises `PsCliError` naming the specific missing/invalid property (never a
    generic parse error, AC-BI-019) when `path` is missing, unreadable, not
    valid JSON, or fails schema validation. No-ops on a conforming document.
    Called by `handlers.handle_internal_ingest` before `client.ingest_internal()`
    -- a rejection here means zero HTTP calls are ever made (D3/B4).
    """
    document = _read_seed_document(path)
    validator = _load_validator()
    violation = _describe_first_violation(validator, document)
    if violation is None:
        return
    raise PsCliError(msg=f"fixture file at {path} is invalid at '{violation}'")
