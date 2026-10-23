r"""Live Role-quality check on the full CRA ingestion.

``@pytest.mark.llm_live @pytest.mark.cellar_live @pytest.mark.falkordb_live``: ingests the Cyber
Resilience Act (CELEX ``32024R2847``) through the real ``POST /ingestions`` route into an empty,
disposable graph, UNBOUNDED (every extraction unit, unlike ``test_live_capstone_external.py``), and
asserts from the resulting graph that the Role set is a clean list of actors:

1. none of the non-role names the issue reported is a Role (compared on the normalised key);
2. no Role name joins two actors with the word ``or`` (those must be split into two requirements);
3. ``count(Role) + count(unresolved Role PendingReview)`` stays small (at most ``_ROLE_CEILING``),
   which also guards the Role review threshold against flooding the review queue.

It cannot run in CI (live model, Cellar, FalkorDB, credentials; the default run deselects all three
markers). Run it by hand with the environment sourced::

    uv run pytest -m "llm_live and cellar_live and falkordb_live" -n0 \
        ps-service/tests/api/test_live_cra_role_quality.py

A failure feeds back into the extraction prompt (``domain_mapper/prompts.py``), the name
normalisation (``domain_mapper/role_names.py``) or the Role review threshold
(``PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD``, 0.80 until calibrated on a live run). The disposable
graph and the ``cra_*`` instrument graphs are dropped before and after the run; the shared
``policy_system`` graph is never written (its node count is asserted unchanged).
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

import pytest
from company_merge._thresholds import REVIEW_THRESHOLD
from fastapi.testclient import TestClient

from api._fakes import install_compliance_officer_grant
from api._live_config import live_app_config
from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.domain_mapper.role_names import role_name_key
from ps_service.graph_gateway.connection import connect_from_config, select_graph
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.logging import facade
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from falkordb import FalkorDB
    from persistence.provisioned_postgres import Provisioned

    from ps_service.api.ingestion_orchestration import GraphHandle

# Captured at import time, before the autouse `_isolate_logging` fixture strips them.
_CHAT_MODEL = os.environ.get("PS_LLMINTERFACE_MODEL")
_EMBED_MODEL = os.environ.get("PS_LLMINTERFACE_EMBED_MODEL")
_ROLE_REVIEW_THRESHOLD = os.environ.get("PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD", "0.80")

_DISPOSABLE_GRAPH: Final = "policy_system_api_cra_role_quality_test"
_REAL_GRAPH: Final = "policy_system"
_INSTRUMENT_GRAPHS: Final = ("cra_baseline", "cra_native")
_CRA_REQUEST: Final = {"source": "catalog", "celex": "32024R2847", "short_name": "CRA"}

# The names the issue reported as Roles although they are not actors. Compared on `role_name_key`.
_NON_ROLE_NAMES: Final = (
    "The technical documentation",
    "EU declaration of conformity",
    "administrative fines",
    "The notification",
    "regulatory sandboxes",
    "Critical products with digital elements",
    "that body",
    "offender",
    "The person referred to in paragraph 1 of this Article",
)
_NON_ROLE_KEYS: Final = frozenset(role_name_key(name) for name in _NON_ROLE_NAMES)
_JOINS_TWO_ACTORS: Final = re.compile(r"\bor\b", re.IGNORECASE)
# The CRA names well under twenty actors; thirty leaves room for legitimate variety and still
# fails on the ~25-plus fragmented set the issue described plus its review noise.
_ROLE_CEILING: Final = 30

pytestmark = [
    pytest.mark.llm_live,
    pytest.mark.cellar_live,
    pytest.mark.falkordb_live,
    pytest.mark.postgres_live,
    pytest.mark.skipif(
        not _CHAT_MODEL or not _EMBED_MODEL,
        reason="requires .env sourced (PS_LLMINTERFACE_MODEL/_EMBED_MODEL, AZURE_*)",
    ),
]


@dataclass(frozen=True, slots=True)
class _CraRoles:
    """What the three assertions read, captured by the one shared run."""

    role_names: tuple[str, ...]
    unresolved_role_reviews: int
    real_policy_system_before: int
    real_policy_system_after: int


def _rows(graph: GraphHandle, query: str) -> list[list[object]]:
    return cast("list[list[object]]", graph.query(query).result_set)


def _count(graph: GraphHandle, query: str) -> int:
    return cast("int", _rows(graph, query)[0][0])


def _delete_graph_if_exists(db: FalkorDB, name: str) -> None:
    if name in db.list_graphs():
        db.select_graph(name).delete()


@pytest.fixture(scope="module")
def cra_roles(
    tmp_path_factory: pytest.TempPathFactory, provisioned_graph_log: Provisioned
) -> Iterator[_CraRoles]:
    """Ingest the full CRA once into a disposable graph and capture its Roles and Role reviews."""
    assert _CHAT_MODEL is not None  # narrowed by the module skipif
    assert _EMBED_MODEL is not None

    monkeypatch = pytest.MonkeyPatch()
    saved_atexit_registered = facade._atexit_registered  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setenv("PS_LLMINTERFACE_MODEL", _CHAT_MODEL)
    monkeypatch.setenv("PS_LLMINTERFACE_EMBED_MODEL", _EMBED_MODEL)
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_MERGE_THRESHOLD", "0.85")
    monkeypatch.setenv("PS_COMPANYMERGE_CAPABILITY_REVIEW_THRESHOLD", str(REVIEW_THRESHOLD))
    monkeypatch.setenv("PS_COMPANYMERGE_ROLE_REVIEW_THRESHOLD", _ROLE_REVIEW_THRESHOLD)
    # Driven end to end through the real `load_config()`: the env is the graph name's only input.
    monkeypatch.setenv("PS_FALKORDB_GRAPH", _DISPOSABLE_GRAPH)

    config = live_app_config(provisioned_graph_log)
    db = connect_from_config(config)
    real_before = _count(select_graph(db, _REAL_GRAPH), "MATCH (n) RETURN count(n)")

    for stale_graph in (_DISPOSABLE_GRAPH, *_INSTRUMENT_GRAPHS):
        _delete_graph_if_exists(db, stale_graph)
    facade.configure(log_path=tmp_path_factory.mktemp("cra_role_quality") / "run.jsonl")

    install_compliance_officer_grant(monkeypatch, granted=True)
    app = create_app(config)
    install_graph_write_gateway(build_default_graph_write_gateway(config))

    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            "/ingestions", json=_CRA_REQUEST
        )
        if response.status_code != 200:
            pytest.fail(f"CRA ingestion returned {response.status_code}: {response.text}")

        graph = select_graph(db, _DISPOSABLE_GRAPH)
        names = tuple(
            cast("str", row[0])
            for row in _rows(graph, "MATCH (r:Role) RETURN r.name ORDER BY r.name")
        )
        reviews = _count(graph, "MATCH (p:PendingReview {kind: 'Role'}) RETURN count(p)")
        real_after = _count(select_graph(db, _REAL_GRAPH), "MATCH (n) RETURN count(n)")
        yield _CraRoles(names, reviews, real_before, real_after)
    finally:
        install_graph_write_gateway(None)
        monkeypatch.undo()
        for graph_name in (_DISPOSABLE_GRAPH, *_INSTRUMENT_GRAPHS):
            with contextlib.suppress(Exception):
                db.select_graph(graph_name).delete()
        facade.reset_for_tests()
        facade._atexit_registered = saved_atexit_registered  # pyright: ignore[reportPrivateUsage]


def test_no_reported_non_role_name_is_a_role(cra_roles: _CraRoles) -> None:
    """(1) none of the issue's non-actor names was minted as a Role."""
    offenders = [name for name in cra_roles.role_names if role_name_key(name) in _NON_ROLE_KEYS]

    assert offenders == [], f"non-actor Roles minted: {offenders}"


def test_no_role_name_joins_two_actors(cra_roles: _CraRoles) -> None:
    """(2) a name such as 'importer or distributor' must have been split."""
    joined = [name for name in cra_roles.role_names if _JOINS_TWO_ACTORS.search(name)]

    assert joined == [], f"Roles joining two actors with 'or': {joined}"


def test_role_count_plus_open_role_reviews_stays_small(cra_roles: _CraRoles) -> None:
    """(3) few Roles and few open Role reviews (also guards the review floor)."""
    total = len(cra_roles.role_names) + cra_roles.unresolved_role_reviews

    assert total <= _ROLE_CEILING, (
        f"{len(cra_roles.role_names)} Roles + {cra_roles.unresolved_role_reviews} Role reviews "
        f"> {_ROLE_CEILING}: {cra_roles.role_names}"
    )


def test_real_policy_system_graph_node_count_unchanged(cra_roles: _CraRoles) -> None:
    """The shared ``policy_system`` graph is untouched by the run."""
    assert cra_roles.real_policy_system_after == cra_roles.real_policy_system_before
