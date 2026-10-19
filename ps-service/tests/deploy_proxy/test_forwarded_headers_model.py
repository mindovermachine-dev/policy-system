"""Config-shape check: every proxied location sets the forwarded headers itself.

nginx discards all inherited `proxy_set_header` directives in a location that sets any of its
own, so a header set only at server level silently stops being applied. The rendered nginx.conf
is parsed and each `proxy_pass` location must set each forwarded header exactly once.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.integration

LOCATION = re.compile(r"location\s+(?:(?:=|\^~)\s+)?\S+\s*\{")
FORWARDED = ("Host", "X-Forwarded-Host", "X-Forwarded-Port", "X-Forwarded-Proto", "X-Forwarded-For")


def _locations(conf: str) -> list[str]:
    bodies: list[str] = []
    for match in LOCATION.finditer(conf):
        depth = 1
        index = match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(conf[index], 0)
            index += 1
        bodies.append(conf[match.end() : index - 1])
    return bodies


def test_every_proxied_location_sets_each_forwarded_header_once(nginx_conf: str) -> None:
    proxied = [body for body in _locations(nginx_conf) if "proxy_pass" in body]

    assert len(proxied) >= 2
    for body in proxied:
        for header in FORWARDED:
            assert len(re.findall(rf"proxy_set_header {header}\s", body)) == 1, (header, body)


def test_no_forwarded_header_is_set_at_server_level(nginx_conf: str) -> None:
    outside = nginx_conf
    for body in _locations(nginx_conf):
        outside = outside.replace(body, "")

    assert "proxy_set_header" not in outside
    assert "proxy_add_x_forwarded_for" not in nginx_conf
