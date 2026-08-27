#!/usr/bin/env python3
# `env python3`, not a hardcoded interpreter path -- run this script
# directly (`ps-service/src/ps_service/query_engine/cypher_cli.py cypher
# ...`), not via `python3 cypher_cli.py`, so it resolves whatever python3
# is first on PATH (the project .venv, once activated per CONTRIBUTING.md).
# Running it directly also lets the harness allow-tool the script itself
# rather than a python interpreter -- an allowed `python3:*` would let an
# agent freelance raw Cypher via `python3 -c`.
"""PS CLI -- read-only Cypher access to the policy_system graph.

CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH are rejected before execution,
not just discouraged.

PLAN_REVIEWED.md §5, Batch 5 / Increment 7: relocated essentially verbatim
from the original `ps_service/mcp_interface/cypher_cli.py` (Option A, §3 --
the standalone dev CLI survives relocation). Only `cmd_cypher`'s body
changes: it now delegates the guard and execution to
`ps_service.query_engine.cypher_query` instead of inlining the regex and
`graph.query()` call, restoring the original's guard-before-connect
evaluation order (S2 fix) via the shared `is_write_clause` helper.
"""

from __future__ import annotations

import argparse
import json
import sys

from falkordb import FalkorDB

from ps_service.logging import configure
from ps_service.query_engine.cypher_query import (
    _WRITE_CLAUSE_REJECTION_MESSAGE,
    execute_cypher_query,
    is_write_clause,
)
from ps_service.query_engine.errors import QueryEngineExecutionError
from ps_service.query_engine.falkordb_client import GraphHandle


def _connect(args: argparse.Namespace) -> GraphHandle:
    db = FalkorDB(host=args.host, port=args.port)
    return db.select_graph(args.graph)


def _print_rows(columns: list[str], rows: list[list[object]], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps({"columns": columns, "rows": rows, "row_count": len(rows)}, indent=2, default=str))
        return
    if not rows:
        print("(no rows)")
        return
    widths = [
        max(len(str(col)), max((len(str(r[i])) for r in rows), default=0))
        for i, col in enumerate(columns)
    ]
    print("  ".join(str(c).ljust(w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)))


def cmd_cypher(args: argparse.Namespace) -> int:
    # S2 fix: the guard runs BEFORE `_connect(args)` is ever called --
    # restores the original file's guard-then-connect evaluation order. A
    # rejected query never causes a FalkorDB connection to be constructed.
    if is_write_clause(args.query):
        print(f"error: {_WRITE_CLAUSE_REJECTION_MESSAGE}", file=sys.stderr)
        return 1

    graph = _connect(args)
    try:
        result = execute_cypher_query(args.query, graph=graph)
    except QueryEngineExecutionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    _print_rows(result.columns, result.rows, args.format)
    return 0


def _common_flags() -> argparse.ArgumentParser:
    # A parent parser, not top-level-only args: agents put flags in varying
    # positions relative to the subcommand (`ps --format json cypher ...`
    # vs `ps cypher ... --format json`), and only accepting the first form
    # is an agent-unfriendly CLI ergonomics trap. Every leaf subparser
    # below re-attaches one of these so both positions work.
    #
    # SUPPRESS, not a real default: argparse applies each subparser's own
    # defaults into the shared namespace after the parent already parsed,
    # so a real default here would silently stomp a value the top-level
    # parser already set from a flag given before the subcommand. The one
    # real default lives on the top-level parser via set_defaults().
    #
    # Called fresh each time it's attached, deliberately -- `parents=[...]`
    # reuses the exact same Action objects across every parser it's given
    # to, so a single shared instance plus set_defaults() on any one parser
    # mutates the default for all of them.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default=argparse.SUPPRESS, help="FalkorDB host (default: localhost)")
    common.add_argument("--port", type=int, default=argparse.SUPPRESS, help="FalkorDB port (default: 6379)")
    common.add_argument("--graph", default=argparse.SUPPRESS, help="Graph name (default: policy_system)")
    common.add_argument(
        "--format", choices=["text", "json"], default=argparse.SUPPRESS, help="Output format (default: text)"
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ps",
        description="PS CLI -- read-only Cypher access to the policy_system graph.",
        parents=[_common_flags()],
    )
    parser.set_defaults(host="localhost", port=6379, graph="policy_system", format="text")

    sub = parser.add_subparsers(dest="command", required=True)

    p_cypher = sub.add_parser(
        "cypher",
        parents=[_common_flags()],
        help="Run a read-only Cypher query directly",
        description=(
            "Read-only: CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH are "
            "rejected before execution, not just discouraged."
        ),
    )
    p_cypher.add_argument("query", help="A MATCH/RETURN-shaped Cypher query")
    p_cypher.set_defaults(func=cmd_cypher)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Bootstraps the Logging component's process-wide default emitter --
    # `execute_cypher_query` (Batch 4) emits a structured log entry on
    # every branch and requires either an explicit `emitter=` or a
    # `configure()`d default; this script is its own process, never
    # sharing `main.py`'s FastAPI `lifespan` bootstrap, so it must install
    # its own default here, mirroring that same precedent.
    configure()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)  # type: ignore[no-any-return]


if __name__ == "__main__":
    sys.exit(main())
