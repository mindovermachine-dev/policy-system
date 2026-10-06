"""Live FalkorDB proof of the case-insensitive identity queries (issue #193, S3, CHANGES M2/M6).

The fast tests prove the queries' text and parameters through fakes that emulate
case-insensitivity; only a real FalkorDB settles that ``toUpper(n.id)`` actually evaluates as
expected in ``WHERE`` against stored ids of arbitrary case. This file seeds a throwaway graph
and drives the real queries: the legacy celex-less preflight (``_is_already_merged``), the
short_name collision check (``check_short_name_collision``) and the CELEX-existence lookup.

Run with:
`uv run pytest -m falkordb_live ps-service/tests/api/test_ingestion_identity_live.py -q`
(needs FalkorDB on 127.0.0.1:6379; collected but deselected by default). Uses a throwaway graph,
deleted before and after each test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from ps_service.api.ingestion_orchestration import (
    _find_instrument_id_by_celex,  # pyright: ignore[reportPrivateUsage] -- the real CELEX query under test
    _is_already_merged,  # pyright: ignore[reportPrivateUsage] -- the real preflight query under test
    check_short_name_collision,
)
from ps_service.ingestion.falkordb_client import FalkorDB, connect, select_graph

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ps_service.api.ingestion_orchestration import GraphHandle as OrchestrationGraphHandle
    from ps_service.ingestion.falkordb_client import GraphHandle

pytestmark = pytest.mark.falkordb_live

_GRAPH = "ingestion_identity_live_test"
_MINE = "32024R2847"
_OTHER = "32024R0001"

type Seed = Callable[..., OrchestrationGraphHandle]


def _delete(db: FalkorDB) -> None:
    if _GRAPH in db.list_graphs():
        db.select_graph(_GRAPH).delete()


@pytest.fixture
def seed() -> Iterator[Seed]:
    """A factory seeding ``RegulatoryInstrument`` nodes into a fresh throwaway graph."""
    db = connect(host="127.0.0.1", port=6379)
    _delete(db)
    handle: GraphHandle = select_graph(db, _GRAPH)

    def _seed(*nodes: tuple[str, str | None]) -> OrchestrationGraphHandle:
        for node_id, celex in nodes:
            if celex is None:
                handle.query("CREATE (:RegulatoryInstrument {id: $id})", params={"id": node_id})
            else:
                handle.query(
                    "CREATE (:RegulatoryInstrument {id: $id, celex: $celex})",
                    params={"id": node_id, "celex": celex},
                )
        return cast("OrchestrationGraphHandle", handle)

    yield _seed
    _delete(db)


def test_preflight_matches_a_legacy_lowercase_celexless_node_for_the_uppercase_id(
    seed: Seed,
) -> None:
    """M2: a celex-less legacy ``cra-1.0`` is caught when ``CRA-1.0`` is ingested."""
    graph = seed(("cra-1.0", None))

    assert _is_already_merged(graph, "CRA-1.0") is True
    assert _is_already_merged(graph, "cra-1.0") is True
    assert _is_already_merged(graph, "DORA-1.0") is False


def test_preflight_matches_a_mixed_case_id(seed: Seed) -> None:
    """M6: a mixed-case ``Cra-1.0`` -- neither all-lower nor all-upper -- still matches."""
    graph = seed(("Cra-1.0", None))

    assert _is_already_merged(graph, "CRA-1.0") is True


def test_collision_check_finds_an_uppercase_claim_for_a_lowercase_short_name(
    seed: Seed,
) -> None:
    """AC-BI-004: ``cra`` collides with ``CRA-1.0`` held by another CELEX; the same CELEX
    does not collide with itself; a hyphenated extension (``CRA-LEGACY-1.0``) is not a collision.
    """
    graph = seed(("CRA-1.0", _OTHER), ("CRA-LEGACY-1.0", "32024R0002"))

    assert check_short_name_collision(graph, celex=_MINE, short_name="cra") == _OTHER
    assert check_short_name_collision(graph, celex=_OTHER, short_name="cra") is None
    assert check_short_name_collision(graph, celex=_MINE, short_name="nis2") is None


def test_collision_check_finds_a_mixed_case_claim(seed: Seed) -> None:
    """M6: a mixed-case stored id ``Cra-1.0`` held by another CELEX is a collision for ``cra``."""
    graph = seed(("Cra-1.0", _OTHER))

    assert check_short_name_collision(graph, celex=_MINE, short_name="CRA") == _OTHER


def test_celex_lookup_returns_the_id_for_an_ingested_celex_and_none_otherwise(
    seed: Seed,
) -> None:
    """AC-BI-003: the CELEX-existence query finds the node by its ``celex`` property."""
    graph = seed(("CRA-1.0", _MINE))

    assert _find_instrument_id_by_celex(graph, _MINE) == "CRA-1.0"
    assert _find_instrument_id_by_celex(graph, _OTHER) is None
