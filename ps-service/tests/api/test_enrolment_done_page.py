"""The page a browser lands on after passkey enrolment, `GET /enrolled`.

Drives the real composition root (`create_app`) with bearer verification on, against the local
mock OIDC provider, so "needs no token" is proven against the real middleware rather than the
local-test bypass. The Authentik enrolment flows redirect to this page by its relative path; the
blueprint-side assertions live in `tests/scripts/test_authentik_blueprint.py`, and the
end-to-end tests below join the two.
"""

from __future__ import annotations

import re
import shlex
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from fastapi.testclient import TestClient

from ps_cli.modules.parser import build_parser
from ps_service.api import enrolment_done
from ps_service.api.enrolment_done import ENROLMENT_DONE_PATH
from ps_service.config import ServiceConfig
from ps_service.logging import facade
from ps_service.main import create_app

# `mock_oidc_provider_fixture` registers pytest's "mock_oidc_provider" fixture; every test below
# declares a same-named parameter instead of calling it.
from ps_test_support.mock_oidc_provider import (
    mock_oidc_provider_fixture,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

if TYPE_CHECKING:
    from ps_test_support.mock_oidc_provider import MockOidcProvider

REPO_ROOT = Path(__file__).resolve().parents[3]
MAIN_BLUEPRINT = REPO_ROOT / "charts" / "policy-system" / "files" / "authentik-blueprint.yaml"

_AUDIENCE = "ps-service"
_FORBIDDEN_TAGS = frozenset({"a", "button", "form", "input", "script"})
# A command the page tells the user to run: the text of a `<code>` element that starts with
# `ps-cli`. The parser test below requires every `ps-cli` mention to be one of these.
_CLI_COMMAND_IN_CODE = re.compile(r"<code>(ps-cli\b[^<]*)</code>")


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader that keeps every Authentik `!Tag` as its plain value instead of failing."""


def _construct_tag(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> object:
    del tag_suffix
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    assert isinstance(node, yaml.MappingNode)
    return loader.construct_mapping(node, deep=True)


_BlueprintLoader.add_multi_constructor(  # pyright: ignore[reportUnknownMemberType]  # PyYAML: untyped stub
    "!", _construct_tag
)


class _TagCollector(HTMLParser):
    """Collects the name of every start tag and every attribute name in a document."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.attributes: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        self.attributes.update(name for name, _ in attrs)


def _config(provider: MockOidcProvider) -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1",
        port=8000,
        graceful_shutdown_seconds=10,
        logging_dir=None,
        is_local_test_bypass_active=False,
        public_url="https://ps.example.com",
        auth_issuer=provider.issuer,
        auth_audience=_AUDIENCE,
        authz_bootstrap_owner_subject="first-owner-subject",
        authz_bootstrap_owner_issuer=provider.issuer,
        authentik_api_token="test-authentik-token",
        authentik_base_url="https://authentik.example.com",
    )


def _redirect_target_of_flow(flow_slug: str) -> str:
    """The `target_static` of the redirect stage bound last to the flow with `flow_slug`."""
    with MAIN_BLUEPRINT.open(encoding="utf-8") as f:
        loaded: dict[str, Any] = yaml.load(f, Loader=_BlueprintLoader)  # noqa: S506  # safe subclass
    entries: list[dict[str, Any]] = loaded["entries"]
    flow_id = next(
        e["id"]
        for e in entries
        if e["model"] == "authentik_flows.flow" and e["identifiers"]["slug"] == flow_slug
    )
    bindings = sorted(
        (
            e
            for e in entries
            if e["model"] == "authentik_flows.flowstagebinding"
            and e["identifiers"]["target"] == flow_id
        ),
        key=lambda b: b["identifiers"]["order"],
    )
    stage_id = bindings[-1]["identifiers"]["stage"]
    stage = next(e for e in entries if e.get("id") == stage_id)
    assert stage["model"] == "authentik_stages_redirect.redirectstage"
    target: str = stage["attrs"]["target_static"]
    return target


@pytest.fixture
def client(mock_oidc_provider: MockOidcProvider, configured_logging: Path) -> TestClient:
    del configured_logging
    return TestClient(create_app(_config(mock_oidc_provider)))


def test_done_page_answers_200_with_html_naming_ps_cli_auth_login(client: TestClient) -> None:
    response = client.get(ENROLMENT_DONE_PATH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "passkey" in response.text
    assert "registered" in response.text
    assert "ps-cli auth login" in response.text


def test_every_ps_cli_command_the_done_page_names_parses_with_the_real_cli_parser(
    client: TestClient,
) -> None:
    body = client.get(ENROLMENT_DONE_PATH).text
    commands = _CLI_COMMAND_IN_CODE.findall(body)

    assert commands, "the done page names no ps-cli command"
    assert body.count("ps-cli") == len(commands), "a ps-cli mention sits outside a <code> element"
    for command in commands:
        argv = shlex.split(command)[1:]
        try:
            build_parser().parse_args(argv)
        except SystemExit as exc:
            pytest.fail(f"the done page names {command!r}, which ps-cli rejects (exit {exc.code})")


def test_done_page_contains_no_link_button_form_script_or_denied_text(
    client: TestClient,
) -> None:
    body = client.get(ENROLMENT_DONE_PATH).text
    collector = _TagCollector()
    collector.feed(body)

    assert collector.tags.isdisjoint(_FORBIDDEN_TAGS), collector.tags & _FORBIDDEN_TAGS
    assert "href" not in collector.attributes
    assert "href=" not in body
    assert "denied" not in body.lower()


def test_done_page_needs_no_bearer_token(client: TestClient) -> None:
    response = client.get(ENROLMENT_DONE_PATH)

    assert response.status_code != 401
    assert "www-authenticate" not in response.headers


def test_done_page_emits_no_log_entry(client: TestClient, configured_logging: Path) -> None:
    client.get(ENROLMENT_DONE_PATH)
    facade.reset_for_tests()

    logged = configured_logging.read_text(encoding="utf-8") if configured_logging.exists() else ""
    assert "enrolment_done_page" not in logged
    assert ENROLMENT_DONE_PATH not in logged


@pytest.mark.parametrize("flow_slug", ["ps-passkey-recovery", "ps-invite-enrollment"])
def test_each_enrolment_flow_redirect_target_is_served_as_the_done_page(
    client: TestClient, flow_slug: str
) -> None:
    target = _redirect_target_of_flow(flow_slug)

    response = client.get(target)

    assert response.status_code == 200
    assert "ps-cli auth login" in response.text
    assert "registered" in response.text


def test_done_page_body_is_identical_for_no_token_a_flow_token_a_bearer_token_and_any_query(
    client: TestClient,
) -> None:
    plain = client.get(ENROLMENT_DONE_PATH)
    variants = [
        client.get(ENROLMENT_DONE_PATH, params={"flow_token": "abc123"}),
        client.get(
            ENROLMENT_DONE_PATH,
            params={"next": "https://evil.example/", "q": "<script>alert(1)</script>"},
        ),
        client.get(ENROLMENT_DONE_PATH, headers={"Authorization": "Bearer not-a-token"}),
        client.get(ENROLMENT_DONE_PATH, headers={"Cookie": "authentik_session=x"}),
    ]

    for response in variants:
        assert response.status_code == 200
        assert response.content == plain.content
    for probe in ("abc123", "evil.example", "<script>", "not-a-token"):
        assert probe not in plain.text


def test_done_page_shows_no_user_data(client: TestClient) -> None:
    body = client.get(ENROLMENT_DONE_PATH).text

    assert body == enrolment_done._DONE_PAGE_HTML  # pyright: ignore[reportPrivateUsage]  # the fixed page itself is the expectation


def test_done_page_sets_no_referrer_and_a_deny_all_content_security_policy(
    client: TestClient,
) -> None:
    headers = client.get(ENROLMENT_DONE_PATH).headers

    assert headers["referrer-policy"] == "no-referrer"
    assert headers["content-security-policy"] == (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    assert headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_done_page_rejects_methods_other_than_get(client: TestClient, method: str) -> None:
    response = client.request(
        method, ENROLMENT_DONE_PATH, params={"q": "reflect-me"}, content=b"reflect-me-too"
    )

    assert response.status_code == 405
    assert "reflect-me" not in response.text
