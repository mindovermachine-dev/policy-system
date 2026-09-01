"""ps-cli command handlers and dispatch table.

Mirrors gh-tt's `modules/tt_handlers.py`, which owns both the handler
functions and the `COMMAND_HANDLERS` dispatch dict together. Each handler
takes an already-constructed client satisfying ``PsServiceClientProtocol`` and any parsed CLI
arguments, and prints exactly what the command's AC requires -- nothing more
(L2 ``## ps-cli`` "Silence on success"). Handlers never catch ``PsCliError``
themselves; ``ps_cli.cli.run()`` owns the single catch site (PLAN.md §1 D5/D9).
"""

from __future__ import annotations

import sys
import threading
import uuid
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from ps_cli.http_client import PsServiceClientProtocol

# How often the background poller (`_poll_ingestion_progress`) checks PS Service for the
# run's currently-executing stage (AC-BI-009). A test overrides this via
# `handle_regulations_ingest`'s `poll_interval_seconds` keyword rather than waiting on the
# real interval (PLAN.md §3 Increment 15).
_POLL_INTERVAL_SECONDS = 2.0

# Bound on how long `handle_regulations_ingest` waits for the poller thread to notice
# `stop_event` and exit, before its own final summary prints (PLAN.md §3 Increment 15). The
# poller's own loop granularity is `poll_interval_seconds`, not this value -- this is only a
# safety bound against an unexpectedly slow/stuck thread.
_POLLER_JOIN_TIMEOUT_SECONDS = 5.0


def handle_regulations_list(client: PsServiceClientProtocol) -> None:
    """Print PS Service's curated regulation catalog, one line per regulation.

    Format: ``"{celex}  {title}"``. No run id is printed here -- ``regulations
    list`` is a read-only catalog query, not an ingestion command, so AC-BI-010
    ("run id printed on ingestion completion") does not apply to it (confirmed
    orchestrator decision; see PLAN.md §3 Increment 9).
    """
    result = client.list_regulations()
    for regulation in result.regulations:
        print(f"{regulation.celex}  {regulation.title}")


def _poll_ingestion_progress(
    client: PsServiceClientProtocol,
    run_id: str,
    stop_event: threading.Event,
    poll_interval_seconds: float,
) -> None:
    """Print each newly-observed in-flight stage to stderr until `stop_event` is set.

    Runs on the daemon background thread `handle_regulations_ingest` starts
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


def handle_regulations_ingest(
    celex: str,
    client: PsServiceClientProtocol,
    *,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
) -> None:
    """Ingest a curated EU regulation, identified by `celex`, via PS Service.

    `celex`'s format is already validated by argparse's `type=_celex_type`
    callback (`ps_cli.modules.parser`) before this handler ever runs -- a
    fast-fail that avoids a wasted round trip for input PS Service would
    reject anyway (L1 "Fail Fast at Boundaries"), enforced at parse time
    rather than re-checked here (PLAN.md §1 D10). On success, prints the run
    id, the regulatory instrument id, and each pipeline stage's name and
    status (AC-BI-002, AC-BI-010). A `PsCliError` raised by the client (e.g. a
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
        print(f"{stage.stage}: {stage.status}")


def handle_internal_ingest(fixture_path: str, client: PsServiceClientProtocol) -> None:
    """Ingest an internal-document fixture, identified by `fixture_path`, via PS Service.

    `fixture_path` is a reference to a file on PS Service's own fixtures
    root -- it is never read from, or otherwise touched on, the operator's
    local filesystem (PLAN.md §1 D8). Its shape (non-empty, ends with
    `.json`) is already validated by argparse's `type=_fixture_path_type`
    callback (`ps_cli.modules.parser`) before this handler ever runs -- a
    fast-fail that avoids a wasted round trip for input PS Service would
    reject anyway (L1 "Fail Fast at Boundaries"), enforced at parse time
    rather than re-checked here (PLAN.md §1 D10). On success, prints the run
    id, the regulatory instrument id, and each pipeline stage's name and
    status (AC-BI-010). A `PsCliError` raised by the client (e.g. today's real
    `internal_ingestion_not_implemented` 501, pending issue #54's backend)
    propagates uncaught -- only `ps_cli.cli.run()` catches `PsCliError`
    (PLAN.md §1 D5/D9).
    """
    result = client.ingest_internal(fixture_path)
    print(f"run_id: {result.run_id}")
    print(f"regulatory_instrument_id: {result.regulatory_instrument_id}")
    for stage in result.stages:
        print(f"{stage.stage}: {stage.status}")


def _dispatch_regulations_list(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_regulations_list`'s single-argument signature to the dispatch shape."""
    del args
    handle_regulations_list(client)


def _dispatch_regulations_ingest(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_regulations_ingest`'s signature to the dispatch shape, passing `celex`."""
    handle_regulations_ingest(cast("str", args.celex), client)


def _dispatch_internal_ingest(args: argparse.Namespace, client: PsServiceClientProtocol) -> None:
    """Adapt `handle_internal_ingest`'s signature to the dispatch shape, passing `fixture_path`."""
    handle_internal_ingest(cast("str", args.fixture_path), client)


DISPATCH: dict[str, Callable[[argparse.Namespace, PsServiceClientProtocol], None]] = {
    "regulations_list": _dispatch_regulations_list,
    "regulations_ingest": _dispatch_regulations_ingest,
    "internal_ingest": _dispatch_internal_ingest,
}
