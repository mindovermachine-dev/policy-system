"""Config-shape model of the evaluator proxy's denial of `/metrics`.

Reuses the location parser and resolver of the allowlist model: the rendered nginx.conf is
parsed and nginx's documented precedence is applied after its own URI normalisation
(percent-decoding, slash merging, dot-segment removal). Proves the shape offline; it does not
run nginx.
"""

from __future__ import annotations

import pytest
from deploy_proxy.test_allowlist_model import Location, parse_locations, resolve

pytestmark = pytest.mark.integration

DENIED = [
    "/metrics",
    "/metrics/",
    "/metrics/foo/bar",
    "/metrics?x=1",
    "/%6detrics",
    "/%6Detrics/",
    "//metrics",
    "/./metrics",
    "/metrics/../metrics",
    "/x/../metrics",
]


@pytest.fixture(scope="module")
def locations(nginx_conf: str) -> list[Location]:
    return parse_locations(nginx_conf)


@pytest.mark.parametrize("target", DENIED)
def test_metrics_targets_are_denied_by_the_metrics_location(
    locations: list[Location], target: str
) -> None:
    outcome, winner = resolve(locations, target)

    assert outcome == "reject"
    assert (winner.modifier, winner.path) == ("^~", "/metrics")
    assert "return 403" in winner.body
    assert "proxy_pass" not in winner.body


def test_a_different_case_path_is_not_denied_by_nginx_and_matches_no_service_route(
    locations: list[Location],
) -> None:
    assert resolve(locations, "/Metrics")[0] == "ps_service"


def test_only_one_location_mentions_metrics(locations: list[Location]) -> None:
    assert [loc.path for loc in locations if "metrics" in loc.path.lower()] == ["/metrics"]
