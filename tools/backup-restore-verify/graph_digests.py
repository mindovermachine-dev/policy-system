#!/usr/bin/env python3
r"""Digest snapshots of FalkorDB graphs for verifying a backup and restore round trip.

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

# Bounds for the one-off connections these tools open; the service reads the same two
# values from `PS_FALKORDB_CONNECT_TIMEOUT_SECONDS` / `PS_FALKORDB_SOCKET_TIMEOUT_SECONDS`.
_CONNECT_TIMEOUT_SECONDS = 5.0
_SOCKET_TIMEOUT_SECONDS = 30.0

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ps_service.graph_gateway.read_only import DriverConnector, ReadOnlyDatabase

EXIT_FAILURE = 1
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 6379


def open_database(
    host: str,
    port: int,
    *,
    connect: DriverConnector | None = None,
) -> ReadOnlyDatabase:
    """Open FalkorDB through the read-only handle, which cannot write.

    Args:
        host: FalkorDB host name or address.
        port: FalkorDB TCP port.
        connect: Optional driver factory taking `(host, port)` and both timeouts; `None` uses the
            real driver.
            Tests pass a fake here.

    Returns:
        A read-only database handle bound to `host`/`port`.
    """
    return open_read_only_database(
        host,
        port,
        connect=connect,
        socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
    )


def snapshot_digests(database: ReadOnlyDatabase, graphs: Sequence[str]) -> dict[str, str]:
    """Compute the canonical content digest of each named graph.

    Args:
        database: Read-only handle to the FalkorDB instance holding the graphs.
        graphs: Graph names to digest; an absent graph digests as an empty graph.

    Returns:
        Mapping of graph name to its `sha256:<hex>` digest, in the order of `graphs`.
    """
    return {name: canonical_digest(database.select_graph(name)) for name in graphs}


def compare_snapshots(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Describe every difference between two digest snapshots.

    Args:
        before: Graph name to digest mapping taken first.
        after: Graph name to digest mapping taken second.

    Returns:
        One human-readable line per graph that is missing from `after`, extra in `after` or
        whose digest differs, ordered by graph name; empty when the snapshots are equal.
    """
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
    parser = argparse.ArgumentParser(
        description="Write and compare per-graph content digests (backup/restore verification)"
    )
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
    connect: DriverConnector | None = None,
) -> int:
    """Run the `snapshot` or `compare` command.

    Args:
        argv: Command-line arguments without the program name; `None` reads `sys.argv`.
        connect: Optional driver factory for tests, forwarded to `open_database`.

    Returns:
        0 on success (snapshot written, or snapshots equal); 1 when `snapshot` has no
        `--graph`, when `compare` finds differences, or when argument parsing fails.

    Raises:
        OSError: The snapshot file cannot be read or written.
        json.JSONDecodeError: A `compare` input file is not valid JSON.
        TypeError: A `compare` input file does not hold a JSON object.

    Side Effects:
        `snapshot` writes the `--out` JSON file and logs one event line per graph to stderr;
        `compare` logs each difference to stderr. FalkorDB is only read.
    """
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
