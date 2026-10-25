"""The Ingress allowlist and the routes of the built app agree.

`charts/policy-system/files/ps-service-public-paths.txt` is the list of paths the chart's PS Service
Ingress forwards. Kubernetes `Prefix` matching applies to it, so an entry covers its own path and
everything below it. Every route of the real app must be covered by an entry or be named in the
internal-only list, so a new route cannot become public (or silently unreachable) without a
decision, and no internal-only path may be covered by any entry.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from ps_service.config import ServiceConfig
from ps_service.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[3]
ALLOWLIST_FILE = REPO_ROOT / "charts" / "policy-system" / "files" / "ps-service-public-paths.txt"

# Routes that must never be reachable through the public Ingress: the orchestrator's liveness and
# readiness probes and the anonymous metrics endpoint, all in-cluster only, and the interactive API
# documentation, which no client needs on the public origin.
INTERNAL_ONLY_PATHS = (
    "/livez",
    "/readyz",
    "/metrics",
    "/docs",
    "/docs/oauth2-redirect",
    "/openapi.json",
    "/redoc",
)


_RETIRED_PROBE_PATHS = tuple(f"/{name}" for name in ("health", "ready"))


def _allowlist_header() -> str:
    lines = ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if line.startswith("#"))


def _built_app_route_paths() -> list[str]:
    app = create_app(
        ServiceConfig(
            host="127.0.0.1",
            port=8000,
            graceful_shutdown_seconds=10,
            logging_dir=None,
            is_local_test_bypass_active=True,
            authentik_api_token="test-authentik-token",
            authentik_base_url="https://authentik.example.com",
        )
    )
    return sorted(set(_walk_route_paths(cast("Iterable[object]", app.routes))))


def _walk_route_paths(routes: Iterable[object]) -> Iterable[str]:
    """Yield the path of every route, descending into included routers.

    An included router appears in `app.routes` as one opaque entry that has no path of its own and
    holds the routes it was built from in `original_router`.
    """
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            yield path
            continue
        original_router = getattr(route, "original_router", None)
        inner = getattr(original_router, "routes", None)
        if inner is not None:
            yield from _walk_route_paths(cast("Iterable[object]", inner))


def _read_allowlist() -> list[str]:
    entries: list[str] = []
    for line in ALLOWLIST_FILE.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            entries.append(entry)
    return entries


def _literal_prefix(route_path: str) -> str:
    """The route's path up to its first path parameter, without a trailing slash."""
    return route_path.split("{", 1)[0].rstrip("/")


def _entry_covers(entry: str, route_path: str) -> bool:
    """Whether an allowlist entry matches the route under Kubernetes `Prefix` semantics."""
    prefix = _literal_prefix(route_path)
    base = entry.rstrip("/")
    return prefix == base or prefix.startswith(base + "/")


def _unlisted_routes(
    route_paths: Iterable[str], entries: Sequence[str], internal_only: Sequence[str]
) -> list[str]:
    return [
        route
        for route in route_paths
        if route not in internal_only and not any(_entry_covers(entry, route) for entry in entries)
    ]


def test_every_app_route_is_public_or_internal_only() -> None:
    unlisted = _unlisted_routes(_built_app_route_paths(), _read_allowlist(), INTERNAL_ONLY_PATHS)

    assert unlisted == []


def test_every_allowlist_entry_covers_a_real_route() -> None:
    routes = _built_app_route_paths()

    dead = [
        entry for entry in _read_allowlist() if not any(_entry_covers(entry, r) for r in routes)
    ]

    assert dead == []


def test_no_internal_only_path_is_covered_by_an_allowlist_entry() -> None:
    entries = _read_allowlist()

    exposed = [
        path
        for path in INTERNAL_ONLY_PATHS
        if any(_entry_covers(entry, path) or _entry_covers(entry, path + "/x") for entry in entries)
    ]

    assert exposed == []


def test_allowlist_has_no_catch_all_entry() -> None:
    assert "/" not in _read_allowlist()


def test_an_unlisted_new_route_is_reported() -> None:
    routes = [*_built_app_route_paths(), "/brand-new-route"]

    unlisted = _unlisted_routes(routes, _read_allowlist(), INTERNAL_ONLY_PATHS)

    assert unlisted == ["/brand-new-route"]


def test_the_allowlist_lists_status_and_no_probe_path() -> None:
    entries = _read_allowlist()

    assert "/status" in entries
    for probe in ("/livez", "/readyz", *_RETIRED_PROBE_PATHS):
        assert probe not in entries, probe


def test_the_allowlist_header_names_status_and_not_the_probes() -> None:
    header = _allowlist_header()

    assert "/status" in header
    for probe in ("/livez", "/readyz", *_RETIRED_PROBE_PATHS):
        assert probe not in header, probe
