"""Config-shape model of the evaluator proxy's /auth/ allowlist.

The rendered nginx.conf is parsed and a small resolver applies nginx's documented location
precedence (exact match, then the longest prefix; `^~` stops regex matching) after nginx's own
URI normalisation (percent-decoding, slash merging, dot-segment removal). It proves the shape of
the configuration offline; it does not run nginx.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote

import pytest

pytestmark = pytest.mark.integration

LOCATION = re.compile(r"location\s+(?:(=|\^~)\s+)?(\S+)\s*\{")
GUARD = re.compile(r'if \(\$ps_raw_path ~\* "(?P<pattern>[^"]+)"\) \{ return 403; \}')


@dataclass(frozen=True)
class Location:
    """One server-level location block."""

    modifier: str
    path: str
    body: str

    @property
    def proxies_to_authentik(self) -> bool:
        return "proxy_pass http://authentik;" in self.body


def _block_end(text: str, start: int) -> int:
    depth = 1
    index = start
    while depth:
        char = text[index]
        depth += char == "{"
        depth -= char == "}"
        index += 1
    return index


def parse_locations(conf: str) -> list[Location]:
    """Return the top-level locations of the named server block."""
    server = conf.rindex("server {", 0, conf.index("server_name"))
    server_open = conf.index("{", server) + 1
    server_body = conf[server_open : _block_end(conf, server_open) - 1]
    locations: list[Location] = []
    position = 0
    while match := LOCATION.search(server_body, position):
        end = _block_end(server_body, match.end())
        locations.append(
            Location(match.group(1) or "", match.group(2), server_body[match.end() : end - 1])
        )
        position = end
    return locations


def normalise(raw_path: str) -> str:
    """Apply nginx's URI normalisation to a raw request path."""
    decoded = unquote(raw_path)
    merged = re.sub(r"/{2,}", "/", decoded)
    resolved = posixpath.normpath(merged)
    return resolved + "/" if merged.endswith("/") and resolved != "/" else resolved


def resolve(locations: list[Location], raw_target: str) -> tuple[str, Location]:
    """Return ('authentik' | 'ps_service' | 'reject', winning location) for a request target."""
    raw_path = raw_target.split("?", 1)[0]
    uri = normalise(raw_path)
    exact = [loc for loc in locations if loc.modifier == "=" and loc.path == uri]
    prefixed = [loc for loc in locations if loc.modifier in {"", "^~"} and uri.startswith(loc.path)]
    winner = exact[0] if exact else max(prefixed, key=lambda loc: len(loc.path))
    guard = GUARD.search(winner.body)
    if guard and re.search(guard.group("pattern").replace("\\\\", "\\"), raw_path, re.IGNORECASE):
        return "reject", winner
    if winner.proxies_to_authentik:
        return "authentik", winner
    if "proxy_pass http://ps_service;" in winner.body:
        return "ps_service", winner
    return "reject", winner


@pytest.fixture(scope="module")
def locations(nginx_conf: str) -> list[Location]:
    return parse_locations(nginx_conf)


BLOCKED = [
    "/auth/if/admin/",
    "/auth/api/v3/core/users/",
    "/auth//if/admin/",
    "/auth/static/../if/admin/",
    "/auth/static/%2e%2e/if/admin/",
    "/auth/static/%2E%2E/if/admin/",
    "/auth/static/..;/if/admin/",
    "/auth/static/%5c..%5cif/admin/",
    "/auth/static/%252e%252e/if/admin/",
    "/auth/application/o/%2e%2e/%2e%2e/if/admin/",
    "/auth/devicex",
    "/auth/flows/-/defaultx/",
    "/auth/api/v3/new/route/",
    "/auth",
    "/auth/",
]

REACHES_AUTHENTIK = [
    "/auth/application/o/authorize/",
    "/auth/application/o/ps/.well-known/openid-configuration",
    "/auth/application/o/authorize/?redirect_uri=https%3A%2F%2Fps.test%3A30443%2Fcb",
    "/auth/device",
    "/auth/device/",
    "/auth/device?user_code=ab",
    "/auth/flows/-/default/authentication/",
    "/auth/if/flow/default-authentication-flow/",
    "/auth/api/v3/flows/executor/default-authentication-flow/?query=next%3D%2Fauth%2F",
    "/auth/api/v3/root/config/",
    "/auth/api/v3/core/brands/current/",
    "/auth/static/dist/flow.js",
]


@pytest.mark.parametrize("target", BLOCKED)
def test_non_allowlisted_auth_targets_are_rejected(locations: list[Location], target: str) -> None:
    assert resolve(locations, target)[0] == "reject"


@pytest.mark.parametrize("target", REACHES_AUTHENTIK)
def test_allowlisted_auth_targets_reach_authentik(locations: list[Location], target: str) -> None:
    assert resolve(locations, target)[0] == "authentik"


def test_a_different_case_prefix_is_not_an_auth_path(locations: list[Location]) -> None:
    assert resolve(locations, "/AUTH/if/admin/")[0] == "ps_service"


def test_every_line_of_the_shared_file_is_reachable(
    locations: list[Location], allowlisted_paths: list[str]
) -> None:
    for path in allowlisted_paths:
        probe = path if path.endswith("/") else path + "/x"
        assert resolve(locations, probe)[0] == "authentik", path


def test_the_only_auth_locations_that_proxy_come_from_the_shared_file(
    locations: list[Location], allowlisted_paths: list[str]
) -> None:
    proxied = {loc.path.rstrip("/") for loc in locations if loc.proxies_to_authentik}

    assert proxied == {path.rstrip("/") for path in allowlisted_paths}


def test_the_bare_landing_paths_answer_with_the_static_body(locations: list[Location]) -> None:
    for target in ("/auth", "/auth/"):
        _, winner = resolve(locations, target)
        assert "Signed in. Open the Policy System from your client." in winner.body
