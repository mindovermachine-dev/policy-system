#!/usr/bin/env python3
r"""Maintainer CLI shim: curate one already-ingested instrument (issue #66, D4).

Thin wrapper over `ps_service.export.export_instrument.export_instrument` --
the real orchestration (embed -> serialize -> checksum -> manifest ->
catalog.json) lives there; this script only parses arguments, resolves a
real FalkorDB connection and a real embedding transport, and reports the
outcome. Mirrors `tools/graph-ingestion/load_graph.py`'s/`tools/
curated-export/migrate_engineering_practices.py`'s own CLI-shim shape
(argparse, `PS_FALKORDB_HOST`/`PS_FALKORDB_PORT` env-driven connection
defaults, a top-level connection guard printing a hint and exiting non-zero).

Unlike every other script in `tools/`, this one always needs a real,
configured LLM Provider -- Export's D7 embedding backfill is the one place
in this whole feature that calls out to an LLM (Restore never does, by
design, so it never needs one). Run it against an already-ingested source,
e.g.:

    uv run tools/curated-export/export_instrument.py \\
        --short-name CRA --instrument-id 32024R2847 --version "1.0" \\
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
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from falkordb import FalkorDB

from ps_service.domain_mapper.falkordb_client import baseline_graph_name
from ps_service.export.errors import ExportSourceGraphError
from ps_service.export.export_instrument import InstrumentDescriptor, export_instrument
from ps_service.export.falkordb_connection import graph_query_handle
from ps_service.ingestion.falkordb_client import native_graph_name
from ps_service.llm_interface.client import default_embedding_caller
from ps_service.llm_interface.errors import LlmProviderError
from ps_service.logging.facade import configure as configure_logging

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Literal

    from ps_service.llm_interface.client import EmbeddingCaller

# Connection defaults, env-driven -- same PS_FALKORDB_HOST/PS_FALKORDB_PORT ps-service and
# every other tools/ script reads (see .env.example): one name repo-wide.
DEFAULT_HOST = os.environ.get("PS_FALKORDB_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("PS_FALKORDB_PORT", "6379"))

# tools/curated-export/export_instrument.py -> parents[2] is the repo root -- mirrors
# migrate_engineering_practices.py's DEFAULT_SEED_FILE resolution exactly.
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
    parser.add_argument("--short-name", required=True, help="e.g. CRA, GDPR")
    parser.add_argument("--instrument-id", required=True, help="e.g. 32024R2847")
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
    argv: Sequence[str] | None = None, *, call_embedding: EmbeddingCaller | None = None
) -> int:
    """Parse args, connect to real FalkorDB, and run `export_instrument` for real.

    `call_embedding` is a test-only injection seam (no CLI flag exposes it --
    there is no way to serialize a fake Python callable through argv): left
    `None`, a real invocation always calls the real `default_embedding_caller`
    (real LiteLLM/provider traffic). Returns a process exit code.
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
        short_name=args.short_name,
        instrument_id=args.instrument_id,
        version=args.version,
        celex=args.celex,
        title=args.title,
        source_type=cast("Literal['external', 'internal']", args.source_type),
        jurisdiction=args.jurisdiction,
    )

    baseline_name = baseline_graph_name(descriptor.short_name)
    native_name = native_graph_name(descriptor.short_name)

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

    try:
        manifest = export_instrument(
            descriptor,
            baseline_graph=graph_query_handle(db, baseline_name),
            native_graph=graph_query_handle(db, native_name),
            embed_model=args.embed_model,
            repo_root=args.repo_root,
            packaged_copy_path=args.packaged_copy_path,
            call_embedding=(
                call_embedding if call_embedding is not None else default_embedding_caller
            ),
        )
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
