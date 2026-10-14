"""The frozen near-miss resolution statements and the interpreter that applies them.

The frozen `pending_review.resolve_review` read a review, checked that both of its nodes still
existed, and then issued ONE statement that picked a winner with a `CASE`, re-pointed five edge
families with `FOREACH ... MERGE` blocks and `DETACH DELETE`d the loser and the review. This
module keeps those statements verbatim (pinned by sha256 of the rendered text, taken at freeze)
and interprets them clause by clause against a node/edge dictionary pair, so the gateway path
can be compared with what they did.

The interpreter is not a Cypher engine. It parses the frozen skeleton with the winner predicate
left open (`coalesce` or none on each side, `<=` or `<`) and evaluates that predicate with Cypher's
three-valued logic: a comparison with a null side is null, and a `CASE WHEN null` takes the `ELSE`
branch. That is what lets the mutation tests below show each clause of the predicate matters. Any
statement outside the frozen skeleton raises.

Met modulo edge identity; the live digest and live interpreter agreement are not verifiable here
(no FalkorDB in this sandbox): the `falkordb_live` tests stay as the live anchor.
"""

from __future__ import annotations

import re
from typing import cast

__all__ = [
    "FROZEN_RESOLVE_HASHES",
    "RESOLVE_EXISTENCE_TEMPLATE",
    "RESOLVE_FIND_REVIEW_QUERY",
    "RESOLVE_MERGE_TEMPLATE",
    "WINNER_PREDICATE",
    "ResolveInterpretation",
    "interpret_resolve_statement",
    "render_resolve_merge",
]

RESOLVE_FIND_REVIEW_QUERY = "MATCH (r:PendingReview {id: $review_id}) RETURN r.kind"

# Verbatim copies of `pending_review._MERGE_EXISTENCE_CHECK_QUERY_TEMPLATE` and
# `_MERGE_QUERY_TEMPLATE` as of commit 006c08f.
RESOLVE_EXISTENCE_TEMPLATE = (
    "MATCH (r:PendingReview {{id: $review_id}}) "
    "OPTIONAL MATCH (a:{kind} {{id: r.incoming_id}}) "
    "WHERE coalesce(a.status, 'active') <> 'merged' "
    "OPTIONAL MATCH (b:{kind} {{id: r.nearest_existing_id}}) "
    "WHERE coalesce(b.status, 'active') <> 'merged' "
    "RETURN r.incoming_id AS incoming_id, r.nearest_existing_id AS nearest_existing_id, "
    "a IS NOT NULL AS incoming_exists, b IS NOT NULL AS existing_exists"
)
WINNER_PREDICATE = "coalesce(a.created_at, '') <= coalesce(b.created_at, '')"
RESOLVE_MERGE_TEMPLATE = (
    "MATCH (a:{kind} {{id: $incoming_id}}), (b:{kind} {{id: $nearest_existing_id}}) "
    "WITH a, b, "
    "CASE WHEN coalesce(a.created_at, '') <= coalesce(b.created_at, '') "
    "THEN a ELSE b END AS winner, "
    "CASE WHEN coalesce(a.created_at, '') <= coalesce(b.created_at, '') "
    "THEN b ELSE a END AS loser "
    "OPTIONAL MATCH (o:Obligation)-[:REQUIRES]->(loser) "
    "FOREACH (_x IN CASE WHEN o IS NOT NULL THEN [1] ELSE [] END | "
    "MERGE (o)-[:REQUIRES]->(winner)) "
    "WITH DISTINCT winner, loser "
    "OPTIONAL MATCH (loser)-[:GOVERNED_BY]->(p:Policy) "
    "FOREACH (_x IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END | "
    "MERGE (winner)-[:GOVERNED_BY]->(p)) "
    "WITH DISTINCT winner, loser "
    "OPTIONAL MATCH (c:Capability)-[:GOVERNED_BY]->(loser) "
    "FOREACH (_x IN CASE WHEN c IS NOT NULL THEN [1] ELSE [] END | "
    "MERGE (c)-[:GOVERNED_BY]->(winner)) "
    "WITH DISTINCT winner, loser "
    "OPTIONAL MATCH (loser)-[:SUPPORTED_BY]->(s:Standard) "
    "FOREACH (_x IN CASE WHEN s IS NOT NULL THEN [1] ELSE [] END | "
    "MERGE (winner)-[:SUPPORTED_BY]->(s)) "
    "WITH DISTINCT winner, loser "
    "OPTIONAL MATCH (t:Capability)-[:MERGED_INTO]->(loser) "
    "FOREACH (_x IN CASE WHEN t IS NOT NULL THEN [1] ELSE [] END | "
    "MERGE (t)-[:MERGED_INTO]->(winner)) "
    "WITH DISTINCT winner, loser "
    "MATCH (rev:PendingReview {{id: $review_id}}) "
    "DETACH DELETE loser, rev "
    "RETURN winner.id AS winner_id, loser.id AS loser_id"
)

FROZEN_RESOLVE_HASHES: dict[str, str] = {
    "find_review": "ad95c2e5aac6d1e7033a8e7ce99e291be9beea724d6107e2e04a6e5266cbc5f4",
    "existence_capability": "736f0433fc3ca41609ef7812360bdae2abfb07cf95b6aa7e07d9d78d460a74fb",
    "existence_policy": "9c7484eccba61a81aebe59c8623944c752a2c50942c5ad5e1d56e11760a774d1",
    "merge_capability": "09cd64443b77980d115d061587e1a8a00aa3e5f1836561333c5d70a9e18443b0",
    "merge_policy": "a339b3bdd486c6a413663f87edcc1d18c4273acf8a350db65bd1724618dfbdd8",
}
"""sha256 of the rendered statements, taken from `git show HEAD:` (commit 006c08f) at freeze."""


def render_resolve_merge(kind: str) -> str:
    """The merge statement for `kind`, as `_resolve_merge` rendered it."""
    return RESOLVE_MERGE_TEMPLATE.format(kind=kind)


_SIDE = r"(?:coalesce\(%s\.created_at, ''\)|%s\.created_at)"
_PREDICATE = rf"(?P<left>{_SIDE % ('a', 'a')}) (?P<op><=|<) (?P<right>{_SIDE % ('b', 'b')})"


def _skeleton() -> re.Pattern[str]:
    """The frozen merge statement with the winner predicate and the kind left open."""
    rendered = RESOLVE_MERGE_TEMPLATE.format(kind="@KIND@")
    escaped = re.escape(rendered)
    predicate = re.escape(WINNER_PREDICATE)
    assert escaped.count(predicate) == 2
    escaped = escaped.replace(predicate, _PREDICATE, 1)
    escaped = escaped.replace(predicate, r"(?P=left) (?P=op) (?P=right)", 1)
    kind = re.escape("@KIND@")
    escaped = escaped.replace(kind, r"(?P<kind>Capability|Policy)", 1)
    escaped = escaped.replace(kind, r"(?P=kind)")
    return re.compile(f"^{escaped}$")


_MERGE_SKELETON = _skeleton()
_EXISTENCE_SKELETON = re.compile(
    "^"
    + re.escape(RESOLVE_EXISTENCE_TEMPLATE.format(kind="@KIND@"))
    .replace(re.escape("@KIND@"), r"(?P<kind>Capability|Policy)", 1)
    .replace(re.escape("@KIND@"), r"(?P=kind)")
    + "$"
)

NodeKey = tuple[str, str]
EdgeKey = tuple[str, str, str, str, str]


class ResolveInterpretation:
    """What one interpreted resolution statement returned (`rows`) and whether it changed state."""

    def __init__(self, rows: list[list[object]]) -> None:
        self.rows = rows


def interpret_resolve_statement(
    nodes: dict[NodeKey, dict[str, object]],
    edges: dict[EdgeKey, dict[str, object]],
    q: str,
    params: dict[str, object] | None,
) -> ResolveInterpretation | None:
    """Apply `q` to `nodes`/`edges` if it is a frozen resolution statement; else return None.

    Raises:
        AssertionError: `q` is neither a frozen statement nor a recognised variant of the winner
            predicate (the reference cannot claim what it was not taught).
    """
    arguments = params or {}
    if q == RESOLVE_FIND_REVIEW_QUERY:
        review = nodes.get(("PendingReview", cast("str", arguments["review_id"])))
        return ResolveInterpretation([] if review is None else [[review["kind"]]])
    existence = _EXISTENCE_SKELETON.match(q)
    if existence is not None:
        return _existence(nodes, existence.group("kind"), cast("str", arguments["review_id"]))
    merge = _MERGE_SKELETON.match(q)
    if merge is not None:
        return _merge(nodes, edges, merge, arguments)
    return None


def _existence(
    nodes: dict[NodeKey, dict[str, object]], kind: str, review_id: str
) -> ResolveInterpretation:
    review = nodes.get(("PendingReview", review_id))
    if review is None:
        return ResolveInterpretation([])

    def _present(node_id: object) -> bool:
        node = nodes.get((kind, cast("str", node_id)))
        # `WHERE coalesce(x.status, 'active') <> 'merged'` on an OPTIONAL MATCH.
        return node is not None and node.get("status", "active") != "merged"

    incoming, nearest = review["incoming_id"], review["nearest_existing_id"]
    return ResolveInterpretation([[incoming, nearest, _present(incoming), _present(nearest)]])


def _side(node: dict[str, object], *, coalesced: bool) -> object:
    value = node.get("created_at")
    return "" if coalesced and value is None else value


def _incoming_wins(
    incoming: dict[str, object], existing: dict[str, object], merge: re.Match[str]
) -> bool:
    """`CASE WHEN <left> <op> <right> THEN a ELSE b END` chose `a`, with Cypher's null logic."""
    left = _side(incoming, coalesced=merge.group("left").startswith("coalesce"))
    right = _side(existing, coalesced=merge.group("right").startswith("coalesce"))
    if left is None or right is None:
        return False  # comparison with null is null; the CASE takes ELSE
    ordered = cast("str", left) < cast("str", right)
    return ordered or (merge.group("op") == "<=" and left == right)


def _merge(
    nodes: dict[NodeKey, dict[str, object]],
    edges: dict[EdgeKey, dict[str, object]],
    merge: re.Match[str],
    params: dict[str, object],
) -> ResolveInterpretation:
    kind = merge.group("kind")
    incoming_key = (kind, cast("str", params["incoming_id"]))
    existing_key = (kind, cast("str", params["nearest_existing_id"]))
    if incoming_key not in nodes or existing_key not in nodes:
        return ResolveInterpretation([])  # the leading MATCH finds no row: nothing runs
    incoming_wins = _incoming_wins(nodes[incoming_key], nodes[existing_key], merge)
    winner, loser = (incoming_key, existing_key) if incoming_wins else (existing_key, incoming_key)
    _rewire(nodes, edges, winner, loser)
    review_key = ("PendingReview", cast("str", params["review_id"]))
    if review_key not in nodes:
        # The trailing `MATCH (rev ...)` finds no row: the rewires above already happened, nothing
        # is deleted and nothing is returned.
        return ResolveInterpretation([])
    _detach_delete(nodes, edges, (loser, review_key))
    return ResolveInterpretation([[winner[1], loser[1]]])


def _rewire(
    nodes: dict[NodeKey, dict[str, object]],
    edges: dict[EdgeKey, dict[str, object]],
    winner: NodeKey,
    loser: NodeKey,
) -> None:
    """The five `OPTIONAL MATCH ... FOREACH (MERGE ...)` blocks, in order, each over all matches.

    `WITH DISTINCT winner, loser` after each block keeps one row, so every match of a block is
    merged and the next block starts from one row (the C1 fix). A `MERGE` of an edge that exists
    keeps it; the edges have no properties here.
    """
    blocks = (
        ("REQUIRES", "Obligation", True),  # (o:Obligation)-[:REQUIRES]->(loser)
        ("GOVERNED_BY", "Policy", False),  # (loser)-[:GOVERNED_BY]->(p:Policy)
        ("GOVERNED_BY", "Capability", True),  # (c:Capability)-[:GOVERNED_BY]->(loser)
        ("SUPPORTED_BY", "Standard", False),  # (loser)-[:SUPPORTED_BY]->(s:Standard)
        ("MERGED_INTO", "Capability", True),  # (t:Capability)-[:MERGED_INTO]->(loser)
    )
    for edge_type, other_label, inbound in blocks:
        if inbound:
            matched = [key for key in edges if key[0] == edge_type and (key[3], key[4]) == loser]
            matched = [key for key in matched if key[1] == other_label]
            for key in matched:
                _merge_edge(nodes, edges, (edge_type, key[1], key[2], winner[0], winner[1]))
        else:
            matched = [key for key in edges if key[0] == edge_type and (key[1], key[2]) == loser]
            matched = [key for key in matched if key[3] == other_label]
            for key in matched:
                _merge_edge(nodes, edges, (edge_type, winner[0], winner[1], key[3], key[4]))


def _merge_edge(
    nodes: dict[NodeKey, dict[str, object]], edges: dict[EdgeKey, dict[str, object]], key: EdgeKey
) -> None:
    if (key[1], key[2]) in nodes and (key[3], key[4]) in nodes:
        edges.setdefault(key, {})


def _detach_delete(
    nodes: dict[NodeKey, dict[str, object]],
    edges: dict[EdgeKey, dict[str, object]],
    doomed: tuple[NodeKey, ...],
) -> None:
    for key in doomed:
        nodes.pop(key, None)
    for edge in [e for e in edges if (e[1], e[2]) in doomed or (e[3], e[4]) in doomed]:
        del edges[edge]
