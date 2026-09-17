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
import sys
import threading
import uuid
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
    from typing import Literal

    from ps_cli.config import CliConfig
    from ps_cli.http_client import PsServiceClientProtocol

# How often the background poller (`_poll_ingestion_progress`) checks PS Service for the
# run's currently-executing stage (AC-BI-009). A test overrides this via
# `handle_ingest_regulation`'s `poll_interval_seconds` keyword rather than waiting on the
# real interval (PLAN.md §3 Increment 15).
_POLL_INTERVAL_SECONDS = 2.0

# Bound on how long `handle_ingest_regulation` waits for the poller thread to notice
# `stop_event` and exit, before its own final summary prints (PLAN.md §3 Increment 15). The
# poller's own loop granularity is `poll_interval_seconds`, not this value -- this is only a
# safety bound against an unexpectedly slow/stuck thread.
_POLLER_JOIN_TIMEOUT_SECONDS = 5.0

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


def handle_near_misses_list(client: PsServiceClientProtocol) -> None:
    """Print every unresolved near-miss `PendingReview`, one line per review (issue #35, AC-BI-003).

    Format: ``"{id}  {similarity:.3f}  {incoming_text!r} vs {nearest_existing_text!r}"``
    -- a read-only listing, one line per entry, nothing else (L2 "Silence on
    success"). `kind` is parsed off the wire response but not printed here
    (PLAN.md §3 Slice 2's formatting is flagged as non-load-bearing, an
    implementation-agnostic choice within AC-BI-003's "shown with its ID,
    incoming text, existing text, and similarity score" requirement).
    """
    result = client.list_pending_reviews()
    for review in result.reviews:
        print(
            f"{review.id}  {review.similarity:.3f}  "
            f"{review.incoming_text!r} vs {review.nearest_existing_text!r}"
        )


def handle_near_misses_resolve(
    review_id: str, decision: Literal["keep-separate", "merge"], client: PsServiceClientProtocol
) -> None:
    """Resolve one near-miss `PendingReview` (issue #35, AC-BI-004/005/006/007/008/009).

    `--decision`'s argparse `choices` (`ps_cli.modules.parser`) already
    reject any value outside `"keep-separate"`/`"merge"` at parse time,
    before this handler ever runs. A not-found, already-resolved, or (merge
    only) stale `review_id` surfaces as a `PsCliError` raised by
    `client.resolve_review()` (AC-BI-008), propagating uncaught -- only
    `ps_cli.cli.run()` catches `PsCliError` (PLAN.md §1 D5/D9, matching
    every other handler in this module).

    On success, prints one confirmation line: `"cleared review {id}
    (keep-separate)"` for `decision="keep-separate"`, or `"merged {loser_id}
    into {winner_id}"` for `decision="merge"` -- exact wording is a
    non-load-bearing formatting choice (PLAN.md §6 Slice 4).
    """
    result = client.resolve_review(review_id, decision)
    if result.decision == "merge":
        print(f"merged {result.loser_id} into {result.winner_id}")
    else:
        print(f"cleared review {result.review_id} ({result.decision})")


def _poll_ingestion_progress(
    client: PsServiceClientProtocol,
    run_id: str,
    stop_event: threading.Event,
    poll_interval_seconds: float,
) -> None:
    """Print each newly-observed in-flight stage to stderr until `stop_event` is set.

    Runs on the daemon background thread `handle_ingest_regulation` starts
    (AC-BI-009). Polls `client.poll_ingestion_status(run_id)` every
    `poll_interval_seconds`, printing `"{stage}: running"` to `sys.stderr`
    only when `stage` is not `None` and differs from the last stage printed
    -- never repeating an unchanged stage. `stop_event.wait(...)` doubles as
    both the sleep and the shutdown signal, so the loop wakes and exits as
    soon as the main thread's blocking `ingest_catalog()` call returns,
    rather than up to one full interval late. `poll_ingestion_status()`
    never raises (PLAN.md §3 Increment 14) -- a poll failure surfaces here
    as `None` and is silently skipped, never affecting the main thread's
    ingestion result (PLAN.md §1 D4).
    """
    last_stage: str | None = None
    while not stop_event.wait(timeout=poll_interval_seconds):
        stage = client.poll_ingestion_status(run_id)
        if stage is not None and stage != last_stage:
            print(f"{stage}: running", file=sys.stderr)
            last_stage = stage


def handle_ingest_regulation(
    celex: str,
    client: PsServiceClientProtocol,
    *,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
) -> None:
    """Ingest a curated EU regulation, identified by `celex`, via PS Service.

    Before anything else, `_assert_llm_interface_available` runs a pre-flight
    readiness check (issue #75, AC-BI-009..013): a target reporting LLM
    Interface unreachable fails fast here, before `celex` validation's
    round-trip-avoidance even matters, and well before the expensive
    `POST /ingestions` call below.

    `celex`'s format is already validated by argparse's `type=_celex_type`
    callback (`ps_cli.modules.parser`) before this handler ever runs -- a
    fast-fail that avoids a wasted round trip for input PS Service would
    reject anyway (L1 "Fail Fast at Boundaries"), enforced at parse time
    rather than re-checked here (PLAN.md §1 D10). On success, prints the run
    id, the regulatory instrument id, and each pipeline stage's name and
    status (AC-BI-002, AC-BI-010). When a stage's summary reports
    `skipped_units > 0` (currently only the extraction stage ever does), that
    count is appended to the stage's line as `" (skipped_units: {n})"`
    (issue #63, AC-BI-003) -- when it is zero or absent, the line is
    byte-identical to before this behavior was added (AC-BI-004). Likewise,
    when a stage's summary reports `pending_reviews > 0` (currently only the
    merge stage ever does), that count is appended as
    `" (pending_reviews: {n})"` (issue #35, AC-BI-010) -- zero or absent
    leaves the line byte-identical, using the exact same conditional-append
    idiom as `skipped_units`. A `PsCliError` raised by the client (e.g. a
    structured PS Service failure response) propagates uncaught -- only
    `ps_cli.cli.run()` catches `PsCliError` (PLAN.md §1 D5/D9).

    While `ingest_catalog()` blocks (a real ingestion runs for minutes), a
    daemon background thread polls PS Service for the run's
    currently-executing stage and prints stage changes to **stderr**
    (AC-BI-009) -- stdout carries only the three summary lines above,
    byte-identical to before this behavior was added (AC-BI-010).
    `poll_interval_seconds` defaults to `_POLL_INTERVAL_SECONDS` (2.0s); a
    caller (e.g. a test) may override it to avoid waiting on the real
    interval.
    """
    _assert_llm_interface_available(client)
    run_id = uuid.uuid4().hex
    stop_event = threading.Event()
    poller = threading.Thread(
        target=_poll_ingestion_progress,
        args=(client, run_id, stop_event, poll_interval_seconds),
        name="ps-cli-ingest-poller",
        daemon=True,
    )
    poller.start()
    try:
        result = client.ingest_catalog(celex, run_id=run_id)
    finally:
        # Stop and join the poller *before* the summary prints below, so no stderr
        # progress line can ever interleave with stdout's final output (AC-BI-010).
        stop_event.set()
        poller.join(timeout=_POLLER_JOIN_TIMEOUT_SECONDS)
    print(f"run_id: {result.run_id}")
    print(f"regulatory_instrument_id: {result.regulatory_instrument_id}")
    for stage in result.stages:
        line = f"{stage.stage}: {stage.status}"
        skipped_units = stage.summary.get("skipped_units", 0)
        if skipped_units:
            line += f" (skipped_units: {skipped_units})"
        pending_reviews = stage.summary.get("pending_reviews", 0)
        if pending_reviews:
            line += f" (pending_reviews: {pending_reviews})"
        print(line)


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
    the same pre-flight readiness check `handle_ingest_regulation` runs
    (issue #75, AC-BI-009..013): a target reporting LLM Interface unreachable
    fails fast here too, before `client.ingest_internal()`'s network call
    (DD2 -- local validation still runs first, since it's the cheaper check
    and would reject the request regardless of readiness). The already-parsed
    document is then sent directly in the request body (D13/D9) -- the file
    is never read or parsed a second time.

    On success, prints the run id, the regulatory instrument id, and each
    pipeline stage's name and status (AC-BI-010). When the `merge` stage's
    summary reports `pending_reviews > 0` (issue #35, AC-BI-010), that count
    is appended to the stage's line as `" (pending_reviews: {n})"`, mirroring
    `handle_ingest_regulation`'s own `skipped_units` idiom byte-for-byte --
    zero or absent prints the line unchanged. A `PsCliError` raised by
    the client (a structured PS Service failure response) propagates
    uncaught -- only `ps_cli.cli.run()` catches `PsCliError` (PLAN.md §1
    D5/D9).
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
    instrument id and each completed stage's name and status, mirroring
    `handle_ingest_regulation`'s summary-line shape. A `PsCliError` raised
    by `catalog_repo.read_artifact` (missing local instrument directory) or
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


def handle_check_regulations(client: PsServiceClientProtocol) -> None:
    """Sweep every tracked instrument for amendments and re-ingest any found.

    Before anything else, `_assert_llm_interface_available` runs a pre-flight
    readiness check (issue #75, AC-BI-009..013): a target reporting LLM
    Interface unreachable fails fast here, before `client.run_change_check()`'s
    sweep, which would otherwise attempt an LLM-dependent re-ingest for every
    amended instrument found.

    Prints the run id first (issue #73, PLAN.md §1 D8), matching
    `handle_ingest_regulation`'s own `print(f"run_id: {result.run_id}")`
    precedent, then one line per tracked instrument, in the order the sweep
    reported them: `"{instrument_id}: {outcome}"`, with `" ({detail})"`
    appended only when `detail` is not `None`. A generic, outcome-agnostic
    formatter (issue #73, PLAN.md §4 Slice 2, moved forward from Slice 6 per
    CHANGES.md's re-sequencing) -- no per-outcome-value special casing, so
    every bucket a later slice's orchestration produces
    (`amendment_reingested` / `poll_failed` / `not_configured` / `skipped` /
    `reingest_failed`) already prints correctly with zero further ps-cli
    changes. An empty sweep instead prints `"no tracked instruments"`.
    """
    _assert_llm_interface_available(client)
    result = client.run_change_check()
    print(f"run_id: {result.run_id}")
    if not result.instruments:
        print("no tracked instruments")
        return
    for outcome in result.instruments:
        detail_suffix = f" ({outcome.detail})" if outcome.detail is not None else ""
        print(f"{outcome.instrument_id}: {outcome.outcome}{detail_suffix}")


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


def _dispatch_ingest_regulation(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_ingest_regulation`'s signature to the dispatch shape, passing `celex`."""
    handle_ingest_regulation(cast("str", args.celex), client)


def _dispatch_near_misses_list(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_near_misses_list`'s single-argument signature to the dispatch shape."""
    del args
    handle_near_misses_list(client)


def _dispatch_near_misses_resolve(
    args: argparse.Namespace, client: PsServiceClientProtocol
) -> None:
    """Adapt `handle_near_misses_resolve`'s signature to the dispatch shape.

    `args.decision` is guaranteed `"keep-separate"` or `"merge"` by the
    parser's own `choices`.
    """
    handle_near_misses_resolve(
        cast("str", args.review_id),
        cast('Literal["keep-separate", "merge"]', args.decision),
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


def _dispatch_check_regulations(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_check_regulations`'s single-argument signature to the dispatch shape."""
    del args
    handle_check_regulations(client)


DISPATCH: dict[str, Callable[[argparse.Namespace, PsServiceClientProtocol], None]] = {
    "ingest_regulation": _dispatch_ingest_regulation,
    "ingest_document": _dispatch_ingest_document,
    "restore_instrument": _dispatch_restore_instrument,
    "export_instrument": _dispatch_export_instrument,
    "get_health": _dispatch_get_health,
    "check_regulations": _dispatch_check_regulations,
    "near_misses_list": _dispatch_near_misses_list,
    "near_misses_resolve": _dispatch_near_misses_resolve,
}

# Commands that, like `config_*` (`ps_cli.modules.config_handlers.CONFIG_DISPATCH`), must
# never construct a `PsServiceClient` or resolve `.service_url` at all (D13). `ps_cli.
# cli.run()` checks this dict before falling through to `DISPATCH` + `_resolve_client`.
NO_CLIENT_DISPATCH: dict[str, Callable[[argparse.Namespace], None]] = {
    "get_catalog": _dispatch_get_catalog,
}
