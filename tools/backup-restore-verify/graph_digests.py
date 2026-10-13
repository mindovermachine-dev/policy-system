#!/usr/bin/env python3
r"""Digest snapshots of FalkorDB graphs for the backup and restore verification (issue #216).

    graph_digests.py snapshot --graph NAME [--graph NAME ...] --out FILE [--host H] [--port P]
    graph_digests.py compare BEFORE.json AFTER.json

`snapshot` reads each named graph through the gateway's read-only handle and writes
`{"<graph>": "sha256:<hex>"}`, the canonical digest of its logical content. It writes nothing to
FalkorDB. `compare` exits 1 and names every graph that differs, is missing or is extra. The
graphs to snapshot are the logged ones (the distinct `graph_log.entries.graph` values), which
`scripts/verify-backup-restore.sh e2e` reads from PS Postgres.

`--host`/`--port` default to `PS_FALKORDB_HOST`/`PS_FALKORDB_PORT`, then `localhost:6379`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ps_service.graph_gateway.digest import canonical_digest
from ps_service.graph_gateway.read_only import open_read_only_database

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ps_service.graph_gateway.read_only import DriverDatabase, ReadOnlyDatabase

EXIT_FAILURE = 1
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 6379


def open_database(
    host: str,
    port: int,
    *,
    connect: Callable[[str, int], DriverDatabase] | None = None,
) -> ReadOnlyDatabase:
    """Open FalkorDB at `host`/`port` through the read-only handle."""
    return open_read_only_database(host, port, connect=connect)


def snapshot_digests(database: ReadOnlyDatabase, graphs: Sequence[str]) -> dict[str, str]:
    """Return the canonical digest of each named graph (an absent graph digests as empty)."""
    return {name: canonical_digest(database.select_graph(name)) for name in graphs}


def compare_snapshots(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Return one line per graph whose digest differs or that only one snapshot holds."""
    differences: list[str] = []
    for name in sorted(before.keys() | after.keys()):
        if name not in after:
            differences.append(f"graph {name}: missing after (before {before[name]})")
        elif name not in before:
            differences.append(f"graph {name}: extra after (digest {after[name]})")
        elif before[name] != after[name]:
            differences.append(f"graph {name}: digest {before[name]} != {after[name]}")
    return differences


def _read_snapshot(path: str) -> dict[str, str]:
    loaded: object = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        message = f"{path} does not hold a JSON object"
        raise TypeError(message)
    return {str(key): str(value) for key, value in loaded.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Graph digest snapshots for #216 verification")
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot", help="write the digest of each named graph")
    snapshot.add_argument("--graph", action="append", default=[], help="graph name (repeatable)")
    snapshot.add_argument("--out", required=True, help="JSON file to write")
    snapshot.add_argument("--host", default=os.environ.get("PS_FALKORDB_HOST", DEFAULT_HOST))
    snapshot.add_argument(
        "--port", type=int, default=int(os.environ.get("PS_FALKORDB_PORT", str(DEFAULT_PORT)))
    )
    compare = commands.add_parser("compare", help="exit 1 when two snapshots differ")
    compare.add_argument("before")
    compare.add_argument("after")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    connect: Callable[[str, int], DriverDatabase] | None = None,
) -> int:
    """Run the CLI; return the process exit code."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit_request:
        return EXIT_FAILURE if exit_request.code else 0
    if args.command == "snapshot":
        graphs: list[str] = args.graph
        if not graphs:
            sys.stderr.write("snapshot needs at least one --graph\n")
            return EXIT_FAILURE
        database = open_database(args.host, args.port, connect=connect)
        digests = snapshot_digests(database, graphs)
        Path(args.out).write_text(json.dumps(digests, indent=2, sort_keys=True) + "\n", "utf-8")
        for name, digest in sorted(digests.items()):
            sys.stderr.write(f"event=graph_digest graph={name} digest={digest}\n")
        return 0
    differences = compare_snapshots(_read_snapshot(args.before), _read_snapshot(args.after))
    for line in differences:
        sys.stderr.write(line + "\n")
    if differences:
        return EXIT_FAILURE
    sys.stderr.write("event=graph_digests_equal\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
