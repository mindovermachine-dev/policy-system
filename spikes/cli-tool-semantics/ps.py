#!/usr/bin/python3
# Hardcoded, not `env python3` -- the dev environment's default python3 (the
# repo .venv) lacks the falkordb package; only /usr/bin/python3 has it (see
# README.md). This also lets the harness allow-tool the script itself
# (`shell(spikes/cli-tool-semantics/ps.py:*)`) rather than the interpreter,
# since allow-tool matches on the program name only, not arguments -- an
# allowed `python3:*` would let an agent freelance raw Cypher via `python3
# -c`, defeating the point of the spike.
"""PS CLI -- minimal command surface for the cli-tool-semantics spike.

See README.md for the spike this supports. This CLI does not reimplement
any query logic -- it wraps the two mechanisms already proven as deterministic
Python libraries in the query spikes:

  - ``ps query template`` wraps query1/query_mechanism_v1.py (the v1
    parameterized-template NL->Cypher router).
  - ``ps query catalog``  wraps query2/catalog.py (Candidate D, the
    pre-compiled denormalized chain catalog).
  - ``ps capabilities list`` is runtime introspection over the same catalog
    -- the "data lives in CLI" side of the model/data split (AD-6).
  - ``ps cypher`` is the read-only escape hatch for questions the
    deterministic surface can't reach.

The point of the spike is whether an agent picks the right command; this
file's job is only to make the right command discoverable and its output
legible -- no new query logic belongs here.
"""

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "query1"))
sys.path.insert(0, str(_HERE.parent / "query2"))

from falkordb import FalkorDB  # noqa: E402

from query_mechanism_v1 import (  # noqa: E402
    NoTemplateMatch,
    QueryMechanismV1,
    TEMPLATES,
)
from catalog import compile_catalog  # noqa: E402

# Cypher clauses that mutate the graph. Best-effort textual guard for a
# spike CLI, not a security boundary -- same discipline the skill-transfer
# runbook applied by hand ("do not run CREATE/MERGE/DELETE/SET against the
# live graph").
_WRITE_CLAUSE = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|FOREACH)\b", re.I
)


def _connect(args):
    db = FalkorDB(host=args.host, port=args.port)
    return db.select_graph(args.graph)


def _print_rows(columns, rows, fmt):
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


# --------------------------------------------------------------------------
# ps query template <question>
# --------------------------------------------------------------------------


def cmd_query_template(args):
    mech = QueryMechanismV1(host=args.host, port=args.port, graph_name=args.graph)
    try:
        result = mech.ask(args.question)
    except NoTemplateMatch:
        msg = (
            f"NO_TEMPLATE_MATCH: no template recognizes this question.\n"
            f"  Try `ps capabilities list` to check entity names, or "
            f"`ps cypher` as a last resort if no deterministic command fits."
        )
        if args.format == "json":
            print(json.dumps({"error": "NO_TEMPLATE_MATCH", "question": args.question}))
        else:
            print(msg)
        return 1

    if args.format == "json":
        print(
            json.dumps(
                {
                    "template": result.template,
                    "columns": result.columns,
                    "rows": result.rows,
                    "row_count": len(result.rows),
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(f"template: {result.template}")
        _print_rows(result.columns, result.rows, args.format)
    return 0


# --------------------------------------------------------------------------
# ps query catalog <capability-id-or-name>
# --------------------------------------------------------------------------

_CATALOG_COLUMNS = [
    "regulation_id",
    "role_name",
    "obligation_id",
    "obligation_text",
    "requirement_id",
    "capability_id",
    "capability_name",
    "policy_id",
    "policy_status",
    "standard_id",
    "standard_status",
    "control_id",
    "control_status",
    "control_next_review_date",
    "is_current_evidence",
]


def _resolve_capability(catalog, text):
    """Exact id, then exact name, then unique name-substring. No fuzzy
    ranking -- same fail-loud discipline as EntityResolver in
    query_mechanism_v1.py: this command expects a caller who already has an
    id (typically from `ps capabilities list`), and only offers a narrow
    convenience fallback beyond that.
    """
    by_id = {cid: (cid, name) for cid, name, _desc in catalog.all_capabilities}
    if text in by_id:
        return by_id[text]

    t = text.strip().lower()
    by_name = {name.lower(): (cid, name) for cid, name, _desc in catalog.all_capabilities}
    if t in by_name:
        return by_name[t]

    matches = [(cid, name) for cid, name, _desc in catalog.all_capabilities if t in name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = ", ".join(f"{cid} ({name})" for cid, name in matches[:5])
        raise ValueError(f"ambiguous capability {text!r} -- candidates: {names}")
    raise ValueError(f"no capability matches {text!r} -- run `ps capabilities list` to see valid ids/names")


def cmd_query_catalog(args):
    graph = _connect(args)
    catalog = compile_catalog(graph)
    try:
        cap_id, cap_name = _resolve_capability(catalog, args.capability)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    rows = [r for r in catalog.rows if r.capability_id == cap_id]
    out_rows = [
        [
            r.regulation_id,
            r.role_name,
            r.obligation_id,
            r.obligation_text,
            r.requirement_id,
            r.capability_id,
            r.capability_name,
            r.policy_id,
            r.policy_status,
            r.standard_id,
            r.standard_status,
            r.control_id,
            r.control_status,
            r.control_next_review_date,
            r.is_current_evidence,
        ]
        for r in rows
    ]

    if args.format == "json":
        print(
            json.dumps(
                {
                    "resolved": {"id": cap_id, "name": cap_name},
                    "columns": _CATALOG_COLUMNS,
                    "rows": out_rows,
                    "row_count": len(out_rows),
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(f"resolved: {cap_id} ({cap_name})")
        _print_rows(_CATALOG_COLUMNS, out_rows, args.format)
    return 0


# --------------------------------------------------------------------------
# ps capabilities list
# --------------------------------------------------------------------------


def cmd_capabilities_list(args):
    graph = _connect(args)
    catalog = compile_catalog(graph)

    governed_ids = {r.capability_id for r in catalog.rows if r.policy_id}

    entries = []
    for cid, name, desc in sorted(catalog.all_capabilities, key=lambda c: c[1]):
        if args.filter and args.filter.lower() not in name.lower():
            continue
        governed = cid in governed_ids
        if args.ungoverned and governed:
            continue
        entries.append((cid, name, desc, governed))

    if args.format == "json":
        print(
            json.dumps(
                [
                    {"id": cid, "name": name, "description": desc, "governed": gov}
                    for cid, name, desc, gov in entries
                ],
                indent=2,
            )
        )
    else:
        columns = ["capability_id", "name", "governed"]
        rows = [[cid, name, "yes" if gov else "no"] for cid, name, _desc, gov in entries]
        _print_rows(columns, rows, args.format)
    return 0


# --------------------------------------------------------------------------
# ps cypher <query>
# --------------------------------------------------------------------------


def cmd_cypher(args):
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


# --------------------------------------------------------------------------
# ps templates -- introspection over what `ps query template` can recognize,
# so an agent can check command coverage without reading source.
# --------------------------------------------------------------------------


def cmd_templates(args):
    entries = [{"name": name, "pattern": pattern.pattern} for name, pattern, _handler in TEMPLATES]
    if args.format == "json":
        print(json.dumps(entries, indent=2))
    else:
        for e in entries:
            print(f"{e['name']:8} {e['pattern']}")
    return 0


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------


def _common_flags():
    # A parent parser, not top-level-only args: agents put flags in varying
    # positions relative to the subcommand (`ps --format json query ...` vs
    # `ps query template ... --format json`), and only accepting the first
    # form is exactly the kind of CLI-ergonomics trap the spike's README
    # calls out ("CLI output format isn't agent-friendly"). Every leaf
    # subparser below re-attaches one of these so both positions work.
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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ps",
        description=(
            "PS CLI -- deterministic query commands over the policy_system "
            "graph. Prefer `ps query template` or `ps query catalog` over "
            "`ps cypher`; reach for `ps cypher` only when no deterministic "
            "command fits the question."
        ),
        parents=[_common_flags()],
    )
    parser.set_defaults(host="localhost", port=6379, graph="policy_system", format="text")

    sub = parser.add_subparsers(dest="command", required=True)

    query = sub.add_parser("query", help="Ask a question through a deterministic mechanism", parents=[_common_flags()])
    query_sub = query.add_subparsers(dest="query_command", required=True)

    p_template = query_sub.add_parser(
        "template",
        parents=[_common_flags()],
        help="Route a natural-language question through the fixed-template router",
        description=(
            "Matches the question's SHAPE against a fixed set of templates "
            "(structural questions: roles, obligations, requirement text, "
            "policy/standard/control lookups by name, aggregate counts). "
            "Returns NO_TEMPLATE_MATCH rather than guessing if no template "
            "recognizes the question -- that is the expected, correct "
            "response for out-of-scope questions, not a failure to work "
            "around. Run `ps templates` to see every recognized pattern."
        ),
    )
    p_template.add_argument("question", help="The question, verbatim, in natural language")
    p_template.set_defaults(func=cmd_query_template)

    p_catalog = query_sub.add_parser(
        "catalog",
        parents=[_common_flags()],
        help="Look up every regulatory-to-organizational chain through one capability",
        description=(
            "Given a Capability id or name, returns every chain that passes "
            "through it: Regulation -> Role -> Obligation -> (this "
            "Capability) -> Policy -> Standard -> Control, plus the "
            "Requirement text that traces back to it. Use this for chain / "
            "coverage / 'are we compliant' questions anchored on a named "
            "capability. Use `ps capabilities list` first if you don't "
            "already have the exact id."
        ),
    )
    p_catalog.add_argument("capability", help="Capability id (e.g. cap_security_logging_c4d9e2) or name")
    p_catalog.set_defaults(func=cmd_query_catalog)

    capabilities = sub.add_parser(
        "capabilities", help="Introspect the Capability vocabulary", parents=[_common_flags()]
    )
    capabilities_sub = capabilities.add_subparsers(dest="capabilities_command", required=True)
    p_cap_list = capabilities_sub.add_parser(
        "list",
        parents=[_common_flags()],
        help="List every Capability, with governance status",
        description=(
            "Runtime introspection over the live Capability vocabulary -- "
            "use this to discover the exact id/name to pass to `ps query "
            "catalog`, or to check whether a name from a question exists "
            "before concluding it doesn't."
        ),
    )
    p_cap_list.add_argument("--filter", help="Case-insensitive substring match on name")
    p_cap_list.add_argument(
        "--ungoverned", action="store_true", help="Only list capabilities with no governing Policy"
    )
    p_cap_list.set_defaults(func=cmd_capabilities_list)

    p_templates = sub.add_parser(
        "templates",
        parents=[_common_flags()],
        help="List every question pattern `ps query template` recognizes",
    )
    p_templates.set_defaults(func=cmd_templates)

    p_cypher = sub.add_parser(
        "cypher",
        parents=[_common_flags()],
        help="Escape hatch: run a read-only Cypher query directly",
        description=(
            "Use ONLY when no deterministic command (`ps query template`, "
            "`ps query catalog`, `ps capabilities list`) fits the question. "
            "Read-only: CREATE/MERGE/DELETE/SET/REMOVE/DROP/FOREACH are "
            "rejected before execution, not just discouraged."
        ),
    )
    p_cypher.add_argument("query", help="A MATCH/RETURN-shaped Cypher query")
    p_cypher.set_defaults(func=cmd_cypher)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
