"""ps-cli command handlers and dispatch table.

Owns both the handler functions and the `DISPATCH` table that maps a parsed
command name to its handler. Each handler takes an already-constructed client
satisfying `PsServiceClientProtocol` and any parsed CLI arguments, and prints
exactly what the command's contract requires and nothing more (L2 `## ps-cli`
"Silence on success"). Handlers never catch `PsCliError` themselves;
`ps_cli.cli.run()` owns the single catch site.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ps_cli.errors import assert_contract
from ps_cli.intake_validation import validate_local_seed_file
from ps_cli.models import StatusDetail

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from ps_cli.http_client import PsServiceClientProtocol

# The dependency name PS Service's `/status` reports when its LLM provider is unreachable.
# Only this dependency's presence in `unhealthy_dependencies` blocks the ingestion
# pre-flight check -- `cellar_eli` alone never does.
_LLM_INTERFACE_DEPENDENCY_NAME = "llm_interface"


_LOGIN_FOR_DETAIL_HINT = "log in to see dependency detail"


def _assert_llm_interface_available(client: PsServiceClientProtocol) -> None:
    """Fail fast with a `PsCliError` when PS Service reports its LLM Interface as unreachable.

    Uses the service's status check with the dependency names required (the call is
    authenticated, and a reply without names is an error rather than a pass). Only
    `llm_interface` appearing in `unhealthy_dependencies` blocks the command; `cellar_eli`
    alone never does, since it is only needed conditionally for the non-curated CELEX fallback. The
    error message names the dependency only, never the raw recorded error string.
    An unreachable PS Service is reported by `get_status()` itself with distinct
    wording ("Could not reach PS Service...").

    Args:
        client: The PS Service client used to query the service status.

    Returns:
        None when the LLM Interface is not reported unhealthy.

    Raises:
        PsCliError: The LLM Interface is reported unhealthy, the dependency names are
            unavailable (not logged in, or a reply without names), or PS Service is
            unreachable or returns an unexpected status response.

    Side Effects:
        Makes one status request to PS Service.
    """
    status = client.get_status(detail=StatusDetail.REQUIRED)
    unhealthy = status.unhealthy_dependencies
    assert_contract(
        contract=unhealthy is not None,
        msg="status detail unavailable.",
        hint="run `ps-cli auth login` and retry",
    )
    assert_contract(
        contract=_LLM_INTERFACE_DEPENDENCY_NAME not in (unhealthy or ()),
        msg="LLM Interface is unavailable.",
        hint="check `ps-cli get health` and PS Service's LLM provider configuration",
    )


def handle_ingest_document(document_path: Path, client: PsServiceClientProtocol) -> None:
    """Ingest an internal document, read locally from `document_path`, via PS Service.

    The file is validated locally first, then a status pre-flight runs, and only
    then is the already-parsed document sent in the request body (the file is never
    read or parsed twice). Local validation runs before the pre-flight because it is
    the cheaper check and would reject the request regardless of the service status. Either
    failure means the ingestion request is never sent.

    On success it prints the run id, the regulatory instrument id, and each pipeline
    stage's name and status. When the `merge` stage's summary reports
    `pending_reviews > 0`, that count is appended to the stage's line as
    `" (pending_reviews: {n})"`; zero or absent leaves the line unchanged.

    Args:
        document_path: A plain local filesystem path (relative to the current
            directory, or absolute) to the intake JSON document. It is read from
            this machine, never resolved against a server-side or ps-cli root. Its
            format (non-empty, ends with `.json`) is already validated by the
            parser before this handler runs.
        client: The PS Service client to use.

    Returns:
        None. The result is printed.

    Raises:
        PsCliError: The file is missing, unreadable or not a conforming document;
            the LLM Interface is unavailable; PS Service is unreachable or rejects
            the request. Propagated uncaught to `ps_cli.cli.run()`.

    Side Effects:
        Reads `document_path`, makes status and ingestion requests to PS Service,
        and prints the result to stdout.
    """
    document = validate_local_seed_file(document_path)
    _assert_llm_interface_available(client)
    result = client.ingest_internal(document)
    print(f"run_id: {result.run_id}")
    print(f"regulatory_instrument_id: {result.regulatory_instrument_id}")
    for stage in result.stages:
        line = f"{stage.stage}: {stage.status}"
        pending_reviews = stage.summary.get("pending_reviews", 0)
        if pending_reviews:
            line += f" (pending_reviews: {pending_reviews})"
        print(line)


def handle_export_instrument(
    instrument_id: str,
    destination: Path | None,
    client: PsServiceClientProtocol,
) -> None:
    """Export one already-ingested instrument's artifact from PS Service.

    The destination is checked before the export request is made: it must already
    exist as a directory and be writable. The export is then requested and three
    files are written under the destination, overwriting any existing ones:
    `baseline.json` and `native.json` (the base64-decoded blobs, as raw bytes) and
    `manifest.json` (the manifest fields, JSON-encoded). On success it prints the
    exported instrument id, each completed stage's name and status, then the three
    written paths; for an internal-source instrument it also prints a notice that
    the files may hold confidential policy content.

    Args:
        instrument_id: The already-ingested instrument's id. Its format is already
            validated by the parser before this handler runs.
        destination: The directory to write into, or `None` for the current working
            directory.
        client: The PS Service client to use.

    Returns:
        None. The result is printed.

    Raises:
        PsCliError: The destination is not an existing directory or is not
            writable (raised before any request is made); PS Service is
            unreachable or rejects the request. Propagated uncaught to
            `ps_cli.cli.run()`.
        OSError: A file cannot be written to the destination.
        binascii.Error: A returned blob is not valid base64.

    Side Effects:
        Makes an export request to PS Service, writes three files to the
        destination, and prints to stdout.
    """
    resolved_destination = destination or Path.cwd()
    assert_contract(
        contract=resolved_destination.is_dir(),
        msg=f"export destination does not exist or is not a directory: {resolved_destination}",
        hint="pass an existing writable directory, or omit the destination to use the cwd",
    )
    assert_contract(
        contract=os.access(resolved_destination, os.W_OK),
        msg=f"export destination is not writable: {resolved_destination}",
        hint="check the directory's permissions",
    )

    result = client.export_instrument(instrument_id)

    baseline_path = resolved_destination / "baseline.json"
    native_path = resolved_destination / "native.json"
    manifest_path = resolved_destination / "manifest.json"
    baseline_path.write_bytes(base64.b64decode(result.baseline_blob_base64))
    native_path.write_bytes(base64.b64decode(result.native_blob_base64))
    manifest_path.write_text(json.dumps(asdict(result.manifest), indent=2))

    print(f"instrument_id: {result.instrument_id}")
    for stage in result.stages:
        print(f"{stage.stage}: {stage.status}")
    print(str(baseline_path))
    print(str(native_path))
    print(str(manifest_path))
    if result.manifest.source_type == "internal":
        print(
            "NOTICE: this export contains internal-source content -- the file may hold "
            "the organization's own confidential policy content."
        )


def handle_get_health(client: PsServiceClientProtocol) -> None:
    """Report PS Service's reachability, health and readiness from one `GET /status`.

    The summary lines print only once the status request has succeeded, so nothing
    is printed before a call that might still fail. A 200 reply means the process
    answered, so the health line reports `alive`. "Reachable but not ready" is
    reported as a `PsCliError` whose hint lists the unhealthy dependencies, if any
    are named (a not-ready state is not always attributable to one). The status
    request carries the login when there is one; without a login PS Service answers
    the status only and the report points to `ps-cli auth login` for the detail.

    Args:
        client: The PS Service client to use.

    Returns:
        None. On success it prints `reachable: yes`, `health: <status>` and
        `ready: <status>`.

    Raises:
        PsCliError: PS Service is unreachable or returns an unexpected response
            (raised by the client's status check), or it is reachable but its
            readiness is not `ready`.

    Side Effects:
        Makes one status request to PS Service and prints to stdout.
    """
    status = client.get_status()
    health_status = "alive"
    names = status.unhealthy_dependencies
    if names is None:
        hint: str | None = _LOGIN_FOR_DETAIL_HINT
    elif names:
        hint = f"unhealthy dependencies: {', '.join(sorted(names))}"
    else:
        hint = None
    assert_contract(
        contract=status.status == "ready",
        msg=(
            f"PS Service is reachable but not ready "
            f"(health={health_status!r}, ready={status.status!r})."
        ),
        hint=hint,
    )
    print("reachable: yes")
    print(f"health: {health_status}")
    print(f"ready: {status.status}")
    if names is None:
        print(f"💡 {_LOGIN_FOR_DETAIL_HINT}")


def _dispatch_export_instrument(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_export_instrument`'s signature to the `DISPATCH` shape.

    Args:
        args: Parsed arguments; `instrument_id` and `destination` are read.
        client: The PS Service client to use.

    Returns:
        None.

    Raises:
        PsCliError: Propagated from `handle_export_instrument`.
        OSError: Propagated from `handle_export_instrument`.
        binascii.Error: Propagated from `handle_export_instrument`.

    Side Effects:
        Those of `handle_export_instrument`: a PS Service request, three files
        written, and stdout output.
    """
    destination_raw = cast("str | None", args.destination)
    handle_export_instrument(
        cast("str", args.instrument_id),
        Path(destination_raw) if destination_raw is not None else None,
        client,
    )


def _dispatch_ingest_document(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_ingest_document`'s signature to the dispatch shape.

    The `document_path` argument is a plain local filesystem path typed on the
    command line, so no configuration lookup is needed to resolve it.

    Args:
        args: Parsed arguments; `document_path` is read.
        client: The PS Service client to use.

    Returns:
        None.

    Raises:
        PsCliError: Propagated from `handle_ingest_document`.

    Side Effects:
        Those of `handle_ingest_document`: a local file read, PS Service requests,
        and stdout output.
    """
    handle_ingest_document(Path(cast("str", args.document_path)), client)


def _dispatch_get_health(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_get_health`'s single-argument signature to the dispatch shape.

    Args:
        args: Parsed arguments; unused, since `get health` takes none.
        client: The PS Service client to use.

    Returns:
        None.

    Raises:
        PsCliError: Propagated from `handle_get_health`.

    Side Effects:
        Those of `handle_get_health`: PS Service requests and stdout output.
    """
    del args
    handle_get_health(client)


DISPATCH: dict[str, Callable[[argparse.Namespace, PsServiceClientProtocol], None]] = {
    "ingest_document": _dispatch_ingest_document,
    "export_instrument": _dispatch_export_instrument,
    "get_health": _dispatch_get_health,
}

# Commands that, like `config_*` (`ps_cli.modules.config_handlers.CONFIG_DISPATCH`), must
# never construct a `PsServiceClient` or resolve `.service_url` at all. `ps_cli.cli.run()`
# checks this dict before falling through to `DISPATCH` + `_resolve_client`. It is
# currently empty and kept as a standing extension point for a future no-client command,
# like `CONFIG_DISPATCH` and `AUTH_DISPATCH`.
NO_CLIENT_DISPATCH: dict[str, Callable[[argparse.Namespace], None]] = {}
