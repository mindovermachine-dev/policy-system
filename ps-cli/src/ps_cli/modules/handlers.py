"""ps-cli command handlers and dispatch table.

Mirrors gh-tt's `modules/tt_handlers.py`, which owns both the handler
functions and the `COMMAND_HANDLERS` dispatch dict together. Each handler
takes an already-constructed client satisfying ``PsServiceClientProtocol`` and any parsed CLI
arguments, and prints exactly what the command's AC requires -- nothing more
(L2 ``## ps-cli`` "Silence on success"). Handlers never catch ``PsCliError``
themselves; ``ps_cli.cli.run()`` owns the single catch site (PLAN.md §1 D5/D9).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from ps_cli.http_client import PsServiceClientProtocol


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


def handle_regulations_ingest(celex: str, client: PsServiceClientProtocol) -> None:
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
    """
    result = client.ingest_catalog(celex)
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
