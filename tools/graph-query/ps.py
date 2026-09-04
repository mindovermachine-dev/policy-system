#!/usr/bin/env python3
# `env python3`, not a hardcoded interpreter path -- run this script
# directly (`tools/graph-query/ps.py cypher ...`), not via `python3
# tools/graph-query/ps.py`, so it resolves whatever python3 is first on
# PATH (the project .venv, once activated per CONTRIBUTING.md). Running it
# directly also lets the harness allow-tool the script itself
# (`shell(tools/graph-query/ps.py:*)`) rather than a python interpreter --
# an allowed `python3:*` would let an agent freelance raw Cypher via
# `python3 -c`.
"""PS CLI -- read-only Cypher access to the policy_system graph.

CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH are rejected before execution,
not just discouraged.

Deprecated for end-user Q&A: this is a local-dev-only fallback, superseded
by the `policy-system` Claude plugin (`ps-skills/policy-system/`, issue
#53), which ships its own MCP connector talking to a running PS Service
instance instead of a locally-spawned subprocess against a local FalkorDB.
Kept for contributor workflows that want raw local graph access without
standing up a full PS Service instance.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import TYPE_CHECKING, Any, Protocol, cast

from falkordb import FalkorDB

# Connection defaults, env-driven. In the dev container FalkorDB is a separate
# container reachable as `falkordb`, not localhost, so a hardcoded default would
# force a --host flag on every invocation. PS_FALKORDB_HOST/PS_FALKORDB_PORT are
# the same variables ps-service reads (see .env.example): one name repo-wide.
DEFAULT_HOST = os.environ.get("PS_FALKORDB_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("PS_FALKORDB_PORT", "6379"))


if TYPE_CHECKING:
    # falkordb ships no py.typed; these Protocols pin the slice of its surface
    # this script touches so the rest of the module stays precisely typed.
    class _QueryResult(Protocol):
        @property
        def header(self) -> list[list[Any]]: ...
        @property
        def result_set(self) -> list[list[Any]]: ...

    class _Graph(Protocol):
        def query(self, q: str) -> _QueryResult: ...

    class _FalkorDB(Protocol):
        def select_graph(self, name: str) -> _Graph: ...


# Cypher clauses that mutate the graph. Best-effort textual guard, not a
# security boundary -- do not run CREATE/MERGE/DELETE/SET against the live
# graph through any other path either.
_WRITE_CLAUSE = re.compile(r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|FOREACH)\b", re.IGNORECASE)


def _connect(args: argparse.Namespace) -> _Graph:
    db = cast("_FalkorDB", FalkorDB(host=args.host, port=args.port))
    return db.select_graph(args.graph)


def _print_rows(columns: list[Any], rows: list[list[Any]], fmt: str) -> None:
    if fmt == "json":
        print(
            json.dumps(
                {"columns": columns, "rows": rows, "row_count": len(rows)}, indent=2, default=str
            )
        )
        return
    if not rows:
        print("(no rows)")
        return
    widths = [
        max(len(str(col)), max((len(str(r[i])) for r in rows), default=0))
        for i, col in enumerate(columns)
    ]
    print("  ".join(str(c).ljust(w) for c, w in zip(columns, widths, strict=False)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=False)))


def cmd_cypher(args: argparse.Namespace) -> int:
    if _WRITE_CLAUSE.search(args.query):
        print(
            "error: this command is read-only -- query contains a write clause "
            "(CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH). Not executed.",
            file=sys.stderr,
        )
        return 1

    graph = _connect(args)
    try:
        result = graph.query(args.query)
    except Exception as e:  # noqa: BLE001 -- surface any FalkorDB error to the caller
        print(f"error: {e}", file=sys.stderr)
        return 1

    columns = [c[1] for c in result.header] if result.header else []
    rows = [list(r) for r in result.result_set]
    _print_rows(columns, rows, args.format)
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
    common.add_argument(
        "--host", default=argparse.SUPPRESS, help=f"FalkorDB host (default: {DEFAULT_HOST})"
    )
    common.add_argument(
        "--port",
        type=int,
        default=argparse.SUPPRESS,
        help=f"FalkorDB port (default: {DEFAULT_PORT})",
    )
    common.add_argument(
        "--graph", default=argparse.SUPPRESS, help="Graph name (default: policy_system)"
    )
    common.add_argument(
        "--format",
        choices=["text", "json"],
        default=argparse.SUPPRESS,
        help="Output format (default: text)",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ps",
        description="PS CLI -- read-only Cypher access to the policy_system graph.",
        parents=[_common_flags()],
    )
    parser.set_defaults(host=DEFAULT_HOST, port=DEFAULT_PORT, graph="policy_system", format="text")

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
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
