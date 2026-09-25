"""ps-cli command handlers and dispatch table.

Mirrors gh-tt's `modules/tt_handlers.py`, which owns both the handler
functions and the `COMMAND_HANDLERS` dispatch dict together. Each handler
takes an already-constructed client satisfying ``PsServiceClientProtocol`` and any parsed CLI
arguments, and prints exactly what the command's AC requires -- nothing more
(L2 ``## ps-cli`` "Silence on success"). Handlers never catch ``PsCliError``
themselves; ``ps_cli.cli.run()`` owns the single catch site (PLAN.md §1 D5/D9).
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ps_cli import catalog_repo, render
from ps_cli.config import load_config
from ps_cli.errors import assert_contract
from ps_cli.intake_validation import validate_local_seed_file

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from ps_cli.config import CliConfig
    from ps_cli.http_client import PsServiceClientProtocol

# The dependency name PS Service's `/ready` reports when its LLM provider is unreachable
# (issue #75). Only this dependency's presence in `unhealthy_dependencies` blocks
# `ingest regulation`'s pre-flight check (AC-BI-009) -- `cellar_eli` alone never does
# (AC-BI-010).
_LLM_INTERFACE_DEPENDENCY_NAME = "llm_interface"


def _assert_llm_interface_available(client: PsServiceClientProtocol) -> None:
    """Pre-flight check (AC-BI-009..013): fail fast if LLM Interface is unreachable.

    Reuses `check_readiness()` (#68's existing `/ready` client method, AC-BI-011) --
    no new endpoint. If PS Service itself can't be reached, `check_readiness()`
    already raises a `PsCliError` worded distinctly ("Could not reach PS Service...")
    from a confirmed-unavailable LLM Interface (AC-BI-012) -- that failure mode needs
    no extra handling here. Only `llm_interface`'s presence in `unhealthy_dependencies`
    blocks the command (AC-BI-009); `cellar_eli` alone never does (AC-BI-010 --
    warn-only, since it's only conditionally needed for the non-curated CELEX
    fallback). The message names the dependency only, never `dependency_health`'s raw
    recorded error string (AC-BI-013) -- `unhealthy_dependencies` is already just a
    list of names, nothing further to sanitize here.
    """
    readiness = client.check_readiness()
    assert_contract(
        contract=_LLM_INTERFACE_DEPENDENCY_NAME not in readiness.unhealthy_dependencies,
        msg="LLM Interface is unavailable.",
        hint="check PS Service's /ready endpoint and its LLM provider configuration",
    )


def handle_ingest_document(document_path: Path, client: PsServiceClientProtocol) -> None:
    """Ingest an internal document, read locally from `document_path`, via PS Service.

    `document_path` is a plain local filesystem path (relative to the
    operator's cwd, or absolute) -- read directly from this machine, never
    resolved against any server-side or ps-cli-owned root (issue #91). Its
    format (non-empty, ends with `.json`) is already validated by argparse's
    `type=_document_path_type` callback (`ps_cli.modules.parser`) before this
    handler ever runs (L1 "Fail Fast at Boundaries").

    Before any network call, `validate_local_seed_file` reads and validates
    the file at `document_path` against the packaged intake-format JSON
    Schema, returning the parsed document (AC-BI-019). A schema violation (or
    a missing/unreadable local file) raises `PsCliError` here, naming the
    specific problem, and neither `_assert_llm_interface_available` nor
    `client.ingest_internal()` is ever called -- provable by a fake client
    recording zero calls.

    Once local validation succeeds, `_assert_llm_interface_available` runs
    a pre-flight readiness check (issue #75, AC-BI-009..013): a target
    reporting LLM Interface unreachable fails fast here too, before
    `client.ingest_internal()`'s network call (DD2 -- local validation still
    runs first, since it's the cheaper check and would reject the request
    regardless of readiness). The already-parsed document is then sent
    directly in the request body (D13/D9) -- the file is never read or
    parsed a second time.

    On success, prints the run id, the regulatory instrument id, and each
    pipeline stage's name and status (AC-BI-010). When the `merge` stage's
    summary reports `pending_reviews > 0` (issue #35, AC-BI-010), that count
    is appended to the stage's line as `" (pending_reviews: {n})"` -- zero
    or absent prints the line unchanged. A `PsCliError` raised by the client
    (a structured PS Service failure response) propagates uncaught -- only
    `ps_cli.cli.run()` catches `PsCliError` (PLAN.md §1 D5/D9).
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


def handle_get_catalog(config: CliConfig) -> None:
    """Print the local curated-content catalog as a bordered table.

    Reads `config.curated_repo_path`'s on-disk `catalog.json` directly via
    `catalog_repo.read_catalog()` -- no `PsServiceClient` is ever constructed
    (D13: `get catalog` needs no PS Service connection at all, unlike every
    other command in this module). This is the only handler here that takes
    a `CliConfig` instead of a client, by design -- see `ps_cli.cli.run()`'s
    `NO_CLIENT_DISPATCH` branch, which never calls `_resolve_client` for it.
    Format: one row per instrument with columns "Instrument ID", "Title",
    "Source Type", "Jurisdiction" (rendered via `render.print_table()`), with
    ``jurisdiction`` printed as ``"n/a"`` when `None` (an internal source,
    D15). Prints nothing if the catalog is empty.
    """
    entries = catalog_repo.read_catalog(config.curated_repo_path)
    rows = [
        [
            entry.instrument_id,
            entry.title,
            entry.source_type,
            entry.jurisdiction if entry.jurisdiction is not None else "n/a",
        ]
        for entry in entries
    ]
    render.print_table(["Instrument ID", "Title", "Source Type", "Jurisdiction"], rows)


def handle_restore_instrument(
    instrument_id: str,
    client: PsServiceClientProtocol,
    *,
    curated_repo_path: Path,
) -> None:
    """Restore one curated instrument's artifact into PS Service.

    `instrument_id`'s format is already validated by argparse's
    `type=_instrument_id_type` callback (`ps_cli.modules.parser`) before this
    handler ever runs (L1 "Fail Fast at Boundaries"). Reads the artifact
    locally via `catalog_repo.read_artifact` (D5: `ps-cli` reads the artifact
    off `curated_repo_path`, PS Service does the FalkorDB work), then uploads
    it via `client.restore_instrument()`. On success, prints the restored
    instrument id and each completed stage's name and status. A `PsCliError`
    raised by `catalog_repo.read_artifact` (missing local instrument directory) or
    by the client (a structured PS Service rejection) propagates uncaught --
    only `ps_cli.cli.run()` catches `PsCliError` (PLAN.md §1 D5/D9).
    """
    artifact = catalog_repo.read_artifact(curated_repo_path, instrument_id)
    result = client.restore_instrument(artifact)
    print(f"instrument_id: {result.instrument_id}")
    for stage in result.stages:
        print(f"{stage.stage}: {stage.status}")


def handle_export_instrument(
    instrument_id: str,
    destination: Path | None,
    client: PsServiceClientProtocol,
) -> None:
    """Export one already-ingested instrument's artifact from PS Service.

    `instrument_id`'s format is already validated by argparse's
    `type=_instrument_id_type` callback (`ps_cli.modules.parser`) before this
    handler ever runs (L1 "Fail Fast at Boundaries"). `destination` resolves
    to the operator's current working directory when not given (AC-BI-006,
    the parser's own `nargs="?", default=None`).

    Before `client.export_instrument()` is ever called, `resolved_destination`
    is checked fail-fast (AC-BI-008): it must already exist as a directory and
    be writable. Either failure raises `PsCliError` naming the path, with an
    actionable hint -- proven by a fake client whose `export_instrument()`
    raises `AssertionError` if invoked, so a wrongly-late check would fail the
    test loudly rather than silently passing.

    Calls `client.export_instrument()`, then writes the three resulting
    files under the resolved destination with plain, deterministic-overwrite
    writes (no partial-write logic needed): `baseline.json`/`native.json`
    (the base64-decoded blobs, written as raw bytes) and `manifest.json`
    (the manifest's fields, JSON-encoded). On success, prints the exported
    instrument id, each completed stage's name and status, then the three
    written paths -- mirroring `handle_restore_instrument`'s summary-line
    shape, plus the paths. A `PsCliError` raised by the client (a structured
    PS Service rejection) propagates uncaught -- only `ps_cli.cli.run()`
    catches `PsCliError` (PLAN.md §1 D5/D9).
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
    """Report PS Service's reachability, health (`/health`), and readiness (`/ready`).

    An unreachable target (AC-BI-009) or an unexpected response shape
    (AC-BI-010) is already, unconditionally, a `PsCliError` raised from
    inside `check_health()`/`check_readiness()` themselves, before this
    handler's own body runs. "Reachable but not ready" is reported here via
    `assert_contract` (CHANGES.md M3, matching
    `config_handlers.handle_config_use_context`'s precedent) rather than a
    bare `if ... raise PsCliError(...)` -- a smaller, more consistent diff
    with no behavioral difference. `hint` is computed first and is `None`
    when no dependency is currently named unhealthy (§0.3's line-970
    nuance: a not-ready state is not always attributable to a specific
    dependency). On success (the contract holds), `assert_contract` no-ops
    and the three summary lines below print unconditionally (D11) --
    nothing prints before a call that might still fail (AC-BI-004).
    """
    health_status = client.check_health()
    readiness = client.check_readiness()
    hint = (
        f"unhealthy dependencies: {', '.join(sorted(readiness.unhealthy_dependencies))}"
        if readiness.unhealthy_dependencies
        else None
    )
    assert_contract(
        contract=health_status == "alive" and readiness.status == "ready",
        msg=(
            f"PS Service is reachable but not ready "
            f"(health={health_status!r}, ready={readiness.status!r})."
        ),
        hint=hint,
    )
    print("reachable: yes")
    print(f"health: {health_status}")
    print(f"ready: {readiness.status}")


def _dispatch_get_catalog(args: argparse.Namespace) -> None:
    """Adapt `handle_get_catalog`'s signature to the `NO_CLIENT_DISPATCH` shape.

    Calls `load_config()` directly -- never `ps_cli.cli._resolve_client` --
    so no `PsServiceClient` is ever constructed for this command (D13).
    Reads only `.curated_repo_path` off the result; `.service_url` is never
    touched, per D13's "skip `_resolve_client`/`load_config().service_url`
    entirely" requirement.
    """
    context = getattr(args, "context", None)
    handle_get_catalog(load_config(context=context))


def _dispatch_restore_instrument(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_restore_instrument`'s signature to the `DISPATCH` shape.

    Unlike `get_catalog`, `restore instrument` does contact PS Service (D5), so
    `client` here is the real `_resolve_client`-built one -- only the extra
    `curated_repo_path` value is resolved locally via `load_config()`.
    """
    context = getattr(args, "context", None)
    curated_repo_path = load_config(context=context).curated_repo_path
    handle_restore_instrument(
        cast("str", args.instrument_id), client, curated_repo_path=curated_repo_path
    )


def _dispatch_export_instrument(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_export_instrument`'s signature to the `DISPATCH` shape.

    Unlike `restore_instrument`, `export` never reads the local curated
    catalog (D1's own scope point), so no `load_config()` call is needed
    here -- only `args.instrument_id`/`args.destination` are unpacked.
    """
    destination_raw = cast("str | None", args.destination)
    handle_export_instrument(
        cast("str", args.instrument_id),
        Path(destination_raw) if destination_raw is not None else None,
        client,
    )


def _dispatch_ingest_document(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_ingest_document`'s signature to the dispatch shape.

    `document_path` is a plain local filesystem path typed on the command
    line -- no config lookup is needed to resolve it (issue #91 removed the
    server-side/root-relative indirection entirely).
    """
    handle_ingest_document(Path(cast("str", args.document_path)), client)


def _dispatch_get_health(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_get_health`'s single-argument signature to the dispatch shape."""
    del args
    handle_get_health(client)


DISPATCH: dict[str, Callable[[argparse.Namespace, PsServiceClientProtocol], None]] = {
    "ingest_document": _dispatch_ingest_document,
    "restore_instrument": _dispatch_restore_instrument,
    "export_instrument": _dispatch_export_instrument,
    "get_health": _dispatch_get_health,
}

# Commands that, like `config_*` (`ps_cli.modules.config_handlers.CONFIG_DISPATCH`), must
# never construct a `PsServiceClient` or resolve `.service_url` at all (D13). `ps_cli.
# cli.run()` checks this dict before falling through to `DISPATCH` + `_resolve_client`.
NO_CLIENT_DISPATCH: dict[str, Callable[[argparse.Namespace], None]] = {
    "get_catalog": _dispatch_get_catalog,
}
