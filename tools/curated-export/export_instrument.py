#!/usr/bin/env python3
r"""Maintainer CLI shim: curate one already-ingested instrument (issue #66, D4).

Thin wrapper over `ps_service.export.export_instrument.export_instrument` --
the real orchestration (embed -> serialize -> checksum -> manifest ->
catalog.json) lives there; this script only parses arguments, resolves a
real FalkorDB connection, a real embedding transport and (since issue #213)
a Graph Write Gateway and a system audit recorder, and reports the outcome.

The embedding backfill is written through the gateway, never directly: the
script builds its own gateway from the service configuration (so it needs the
PS state Postgres, `PS_STATE_POSTGRES_*`, reachable, like the service), with
`--host`/`--port` applied to the gateway's FalkorDB as well so reads and
writes hit the same server. It records one `export.instrument` system audit
row first (fail-closed: no row, no embedding, no write) and anchors the
backfill group to it. Same CLI-shim shape as `tools/graph-ingestion/load_graph.py`:
argparse, `PS_FALKORDB_HOST`/`PS_FALKORDB_PORT` env-driven connection
defaults, a top-level connection guard printing a hint and exiting non-zero.

Unlike every other script in `tools/`, this one always needs a real,
configured LLM Provider -- Export's D7 embedding backfill is the one place
in this whole feature that calls out to an LLM (Restore never does, by
design, so it never needs one). Run it against an already-ingested source,
e.g.:

    uv run tools/curated-export/export_instrument.py \\
        --short-name CRA --instrument-id CRA-1.0 --version "1.0" \\
        --celex 32024R2847 --title "Cyber Resilience Act" \\
        --source-type external --jurisdiction EU

Requires a running FalkorDB instance with the source `{short}_baseline`/
`{short}_native` graphs already populated (ingestion already run for this
instrument), and `PS_LLMINTERFACE_EMBED_MODEL` set (or `--embed-model`
passed explicitly) plus that provider's own credentials (e.g.
`AZURE_API_KEY`/`AZURE_API_BASE`/`AZURE_API_VERSION`) resolvable by LiteLLM.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from falkordb import FalkorDB

from ps_service.api.export_audit_actions import (
    EXPORT_INSTRUMENT_ACTION,
    EXPORT_INSTRUMENT_RESOURCE_TYPE,
    ExportInstrumentDetails,
)
from ps_service.audit.errors import AuditTrailUnavailableError
from ps_service.config import load_config
from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.export.errors import ExportGraphWriteError, ExportSourceGraphError
from ps_service.export.export_instrument import InstrumentDescriptor, export_instrument
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.graph_gateway.system_audit import build_system_audit_recorder
from ps_service.ingestion.falkordb_client import native_graph_name
from ps_service.llm_interface.client import default_embedding_caller
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.logging.facade import configure as configure_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Literal

    from ps_service.config import ServiceConfig
    from ps_service.graph_gateway.gateway import GraphWriteGateway
    from ps_service.graph_gateway.system_audit import SystemAuditRecorder
    from ps_service.ingestion.graph_submission import GroupSubmitter
    from ps_service.llm_interface.client import EmbeddingCaller

# Connection defaults, env-driven -- same PS_FALKORDB_HOST/PS_FALKORDB_PORT ps-service and
# every other tools/ script reads (see .env.example): one name repo-wide.
DEFAULT_HOST = os.environ.get("PS_FALKORDB_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("PS_FALKORDB_PORT", "6379"))

# tools/curated-export/export_instrument.py -> parents[2] is the repo root -- the
# standard repo-root resolution every tools/ script anchored two directories deep uses.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPO_ROOT = _REPO_ROOT
# CHANGES.md MA3's exact packaged-copy destination -- ps_service.api.curated_content's
# importlib.resources-packaged copy of catalog.json, always under this checkout's own
# ps-service tree regardless of what --repo-root a caller passes for curated-content output.
DEFAULT_PACKAGED_COPY_PATH = (
    _REPO_ROOT / "ps-service" / "src" / "ps_service" / "api" / "curated_content" / "catalog.json"
)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--short-name",
        required=True,
        help="Source graph prefix -- must match the ingested "
        "'{short_name.lower()}_baseline'/'_native' FalkorDB graphs exactly, e.g. CRA, GDPR",
    )
    parser.add_argument(
        "--catalog-short-name",
        default=None,
        help="Display short_name written to manifest.json/catalog.json, if it should "
        "differ from --short-name (e.g. an ingestion-time slug vs. a human-friendly "
        "name). Defaults to --short-name.",
    )
    parser.add_argument(
        "--instrument-id",
        required=True,
        help="Must equal the source baseline graph's own RegulatoryInstrument.id -- "
        "normally '{short_name}-{version}', e.g. CRA-1.0. NOT the CELEX: restore "
        "rejects an artifact whose manifest instrument_id doesn't match the graph's "
        "own id (checked here too, before anything is written).",
    )
    parser.add_argument("--version", required=True, help="e.g. 1.0")
    parser.add_argument("--celex", default=None, help="CELEX id (external sources only)")
    parser.add_argument("--title", required=True, help="Human-readable instrument title")
    parser.add_argument("--source-type", required=True, choices=["external", "internal"])
    parser.add_argument("--jurisdiction", default=None, help="e.g. EU")
    parser.add_argument("--host", default=DEFAULT_HOST, help="FalkorDB host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="FalkorDB port")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=DEFAULT_REPO_ROOT,
        help=f"Where curated-content/ is written (default: {DEFAULT_REPO_ROOT})",
    )
    parser.add_argument(
        "--packaged-copy-path",
        type=Path,
        default=DEFAULT_PACKAGED_COPY_PATH,
        help=f"Second catalog.json destination, MA3 (default: {DEFAULT_PACKAGED_COPY_PATH})",
    )
    parser.add_argument(
        "--embed-model",
        # Read fresh per call (not a module-level default) so an env var set after import,
        # or a test's own monkeypatched environment, is always honored.
        default=os.environ.get("PS_LLMINTERFACE_EMBED_MODEL"),
        help="LiteLLM <provider>/<model> string, e.g. azure/text-embedding-3-large "
        "(default: $PS_LLMINTERFACE_EMBED_MODEL)",
    )
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    call_embedding: EmbeddingCaller | None = None,
    load_service_config: Callable[[], ServiceConfig] = load_config,
    build_gateway: Callable[[ServiceConfig], GraphWriteGateway] = build_default_graph_write_gateway,
    build_audit: Callable[[ServiceConfig], SystemAuditRecorder] = build_system_audit_recorder,
) -> int:
    """Parse args, connect to real FalkorDB, and run `export_instrument` for real.

    `call_embedding` is a test-only injection seam (no CLI flag exposes it --
    there is no way to serialize a fake Python callable through argv): left
    `None`, a real invocation always calls the real `default_embedding_caller`
    (real LiteLLM/provider traffic). `load_service_config`, `build_gateway` and `build_audit`
    are the same kind of seam for the service-configuration, graph-write-gateway and
    audit-recorder composition: left at their defaults a real invocation wires the real ones.
    Returns a process exit code.
    """
    # This script is its own process composition root (like `main.py`): `RouteEmbedding`'s
    # own `log()` call requires a configured default emitter before any `emit_log_entry`
    # call happens (`ps_service.logging.facade`'s own contract) -- configured here, once,
    # exactly like `main.py` does for the long-running service process.
    configure_logging()

    args = _parse_args(argv)

    if not args.embed_model:
        print(
            "No embedding model configured. Pass --embed-model or set "
            "PS_LLMINTERFACE_EMBED_MODEL (e.g. azure/text-embedding-3-large). "
            "Export always needs a real embedding backfill (D7) -- this is the one "
            "step in this whole feature that calls an LLM Provider.",
            file=sys.stderr,
        )
        return 1

    descriptor = InstrumentDescriptor(
        short_name=args.catalog_short_name or args.short_name,
        instrument_id=args.instrument_id,
        version=args.version,
        celex=args.celex,
        title=args.title,
        source_type=cast("Literal['external', 'internal']", args.source_type),
        jurisdiction=args.jurisdiction,
    )

    # Deliberately from args.short_name, not descriptor.short_name -- the source graphs were
    # ingestion-named and don't move just because --catalog-short-name overrides the manifest's
    # display value. export_instrument() itself does no graph selection (see its docstring); this
    # script is the only place these names are resolved.
    baseline_name = baseline_graph_name(args.short_name)
    native_name = native_graph_name(args.short_name)

    try:
        db = FalkorDB(host=args.host, port=args.port)
        db.select_graph(baseline_name).query("RETURN 1")
    except Exception as exc:  # noqa: BLE001 -- top-level connection guard: print a hint and exit non-zero
        print(
            f"FalkorDB connection failed at {args.host}:{args.port}. "
            f"Is FalkorDB running? Error: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        f"Exporting instrument '{descriptor.instrument_id}' ({descriptor.short_name}) "
        f"from '{baseline_name}'/'{native_name}' at {args.host}:{args.port}, "
        f"embedding via '{args.embed_model}'..."
    )

    # The gateway writes the same FalkorDB the reads above hit (--host/--port), and its log is the
    # PS state Postgres of the service configuration. Contacts neither until first used.
    config = dataclasses.replace(
        load_service_config(), falkordb_host=args.host, falkordb_port=args.port
    )
    gateway = build_gateway(config)
    try:
        return _export_through_gateway(
            args,
            descriptor,
            db=db,
            gateway=gateway,
            record_audit=build_audit(config),
            call_embedding=call_embedding,
        )
    finally:
        gateway.stop_reconciler()


def _export_through_gateway(
    args: argparse.Namespace,
    descriptor: InstrumentDescriptor,
    *,
    db: FalkorDB,
    gateway: GroupSubmitter,
    record_audit: SystemAuditRecorder,
    call_embedding: EmbeddingCaller | None,
) -> int:
    """Record the Export audit row, run `export_instrument` and report; return the exit code."""
    baseline_name = baseline_graph_name(args.short_name)
    native_name = native_graph_name(args.short_name)
    try:
        # Fail-closed (issue #213, AC-BI-010): without the row there is no id to anchor the
        # group to, so nothing is embedded or written.
        audit_event_id = record_audit(
            action=EXPORT_INSTRUMENT_ACTION,
            resource_type=EXPORT_INSTRUMENT_RESOURCE_TYPE,
            resource_id=descriptor.instrument_id,
            details=ExportInstrumentDetails(
                instrument_id=descriptor.instrument_id, status="started"
            ).model_dump(),
        )
    except AuditTrailUnavailableError as exc:
        print(
            f"The audit trail is unavailable ({exc}). Nothing was embedded or written. "
            "Is the PS state Postgres (PS_STATE_POSTGRES_*) reachable?",
            file=sys.stderr,
        )
        return 1

    try:
        manifest = export_instrument(
            descriptor,
            baseline_graph=graph_query_handle(db, baseline_name),
            native_graph=graph_query_handle(db, native_name),
            embed_model=args.embed_model,
            repo_root=args.repo_root,
            packaged_copy_path=args.packaged_copy_path,
            submitter=gateway,
            audit_event_id=audit_event_id,
            baseline_graph_name=baseline_name,
            call_embedding=(
                call_embedding if call_embedding is not None else default_embedding_caller
            ),
        )
    except ExportGraphWriteError as exc:
        print(f"The embedding backfill could not be written: {exc}", file=sys.stderr)
        return 1
    except LlmProviderError as exc:
        print(f"LLM provider error while backfilling embeddings: {exc}", file=sys.stderr)
        print(
            "Check PS_LLMINTERFACE_EMBED_MODEL / --embed-model and the configured "
            "provider's own credentials (e.g. AZURE_API_KEY/AZURE_API_BASE/AZURE_API_VERSION).",
            file=sys.stderr,
        )
        return 1
    except ExportSourceGraphError as exc:
        print(
            f"Source graph '{baseline_name}'/'{native_name}' is not exportable: {exc}",
            file=sys.stderr,
        )
        return 1

    instrument_dir = args.repo_root / "curated-content" / manifest.instrument_id
    print("Export succeeded.")
    print(f"  instrument_id:  {manifest.instrument_id}")
    print(f"  short_name:     {manifest.short_name}")
    print(f"  version:        {manifest.version}")
    print(f"  schema_version: {manifest.schema_version}")
    print(f"  baseline_sha256: {manifest.baseline_sha256}")
    print(f"  native_sha256:   {manifest.native_sha256}")
    print(f"  written to:     {instrument_dir}")
    print(f"  catalog.json:   {args.repo_root / 'curated-content' / 'catalog.json'}")
    print(f"  packaged copy:  {args.packaged_copy_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
