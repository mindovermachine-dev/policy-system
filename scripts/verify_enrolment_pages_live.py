#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = ["playwright==1.63.0"]
# ///
"""Live browser check of the passkey enrolment pages on a deployed evaluator release.

Drives a headless Chromium with a virtual passkey (Chrome DevTools WebAuthn domain) through the
ingress of a kind evaluator cluster, and reads Authentik's admin API through a `kubectl
port-forward` with the shared API token from the release's Secret. Each check prints one line,
`PASS <check>`, `FAIL <check>: <reason>` or `SKIP <check> (<reason>)`, and the last line is
`SUMMARY <n> passed, <n> failed, skipped: <names|none>`. Exit status: 0 when nothing failed, 1 on
any FAIL, 2 on a usage error.

Checks (`--only` selects a comma-separated subset; the default runs every check except
`upgrade-precondition`, which holds only before an upgrade from a release whose enrolment flows end
on a deny confirmation stage):

- `upgrade-precondition`: both enrolment flows bind their deny confirmation stage at order 110.
- `upgrade-state:recovery`, `upgrade-state:enrollment`: once Authentik reports the bundled
  blueprint `successful` with a `last_applied_hash` equal to the SHA-512 of the blueprint text in
  the release's ConfigMap (waiting a bounded time for a pending apply, and failing when the applied
  blueprint is still an older one), the passkey recovery (or invite enrollment) flow has exactly
  one binding after user login, a static redirect stage to the done page, and the flow's deny
  confirmation stage does not exist.
- `first-owner`: walks the first-owner enrolment link a fresh deploy printed (`--first-owner-link`)
  to the done page.
- `owner-reset`: runs `deploy-ps-eval.sh --reset-owner-passkey` twice for an owner who already holds
  a passkey, walks each link with a new virtual passkey, and requires the owner's device count to
  be at least 1 after the first walk, 0 after the second reset and exactly 1 after the second walk,
  plus a `login` event and no `logout` event after it. It revokes the owner's passkeys, so it runs
  only with `--allow-owner-reset`.
- `invite`: creates a single-use invitation for a disposable `live-invitee-<hex>` user, walks the
  invite link with a new virtual passkey (username and name typed into the prompt) to the done
  page, and requires the invitee to hold a WebAuthn device, a `login` event and no `logout` event
  after it. The invitee and any unused invitation are deleted afterwards, even after a FAIL.
- `done-page-inputs`: through the ingress, the done page answers five requests (none, a flow
  token, a hostile `next` and script query, a bearer token, an Authentik session cookie) with
  HTTP 200 and byte-identical bodies that reflect none of the inputs, each carrying the page's
  `Content-Security-Policy`, `X-Content-Type-Options` and `Referrer-Policy` headers.
- `login-cancel-hidden`: enrols a disposable invitee as `invite` does, then opens the passkey login
  twice in a fresh browser: once as PS-Cli enters it (a device authorization for `ps-cli`, then its
  `verification_uri_complete`, which redirects to the login flow with a `?next=` query) and once by
  the login flow's own URL. After the invitee's username is typed and the passkey prompt shows, a
  link to Authentik's flow cancel path is attached to the page (carrying the `?next=` query on the
  PS-Cli entry) but none is visible, and no "Not you?" text is visible.
- `lockout`: creates a disposable `lockout-probe-<hex>` user, reads the login reputation policy
  and every reputation score, and seeds a score for the probe through `ak shell` on the Authentik
  worker low enough that the policy's sum over the scores matching the client IP or the username
  stays below its threshold whatever positive score the shared client IP already holds. It then
  types the username into the passkey login and requires the lockout message to be visible with
  no visible cancel link and no visible "Go back". Only the probe's reputation rows and the user
  are deleted afterwards, even after a FAIL; no other reputation row is changed.
- `unrouted-404`: `/auth/`, `/auth/if/user/`, `/auth/if/admin/` and `/auth/flows/-/cancel/` answer
  404 through the ingress.

Every walk also requires each visible web link on the final page to answer something other than
404. A request through the ingress is retried on a transport error only (the Podman port mapping
of a kind cluster drops some connections), never on an HTTP status. The owner is never deleted.
`--resolve-to 127.0.0.1` sends every request for `--host` to that address, as the deploy script's
own `curl --resolve` probes do, when the host's name does not resolve to this machine.

One-time browser install, then a run (sandbox off, the current kube context on the eval cluster):

    uvx --from playwright==1.63.0 playwright install chromium
    uv run --script scripts/verify_enrolment_pages_live.py --host ps.test --port 30443 \
        --ca "$PS_EVAL_STATE_DIR/ca.pem" --owner-email "$E" --allow-owner-reset \
        --artifacts-dir <dir>
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, TYPE_CHECKING, Final, Literal, Protocol, cast
from urllib.parse import quote, urlparse, urlunparse

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence

DONE_PATH: Final = "/enrolled"
REPO_ROOT: Final = Path(__file__).resolve().parents[1]
DEPLOY_EVAL_SCRIPT: Final = REPO_ROOT / "scripts" / "deploy-ps-eval.sh"

UPGRADE_PRECONDITION: Final = "upgrade-precondition"
UPGRADE_STATE_RECOVERY: Final = "upgrade-state:recovery"
UPGRADE_STATE_ENROLLMENT: Final = "upgrade-state:enrollment"
FIRST_OWNER: Final = "first-owner"
OWNER_RESET: Final = "owner-reset"
INVITE: Final = "invite"
DONE_PAGE_INPUTS: Final = "done-page-inputs"
LOGIN_CANCEL_HIDDEN: Final = "login-cancel-hidden"
LOCKOUT: Final = "lockout"
UNROUTED_404: Final = "unrouted-404"
CHECK_NAMES: Final = (
    UPGRADE_PRECONDITION,
    UPGRADE_STATE_RECOVERY,
    UPGRADE_STATE_ENROLLMENT,
    FIRST_OWNER,
    OWNER_RESET,
    INVITE,
    UNROUTED_404,
    DONE_PAGE_INPUTS,
    LOGIN_CANCEL_HIDDEN,
    LOCKOUT,
)
DEFAULT_CHECKS: Final = tuple(name for name in CHECK_NAMES if name != UPGRADE_PRECONDITION)

RECOVERY_FLOW: Final = "ps-passkey-recovery"
ENROLLMENT_FLOW: Final = "ps-invite-enrollment"
RETIRED_DENY_STAGES: Final = {
    RECOVERY_FLOW: "ps-recovery-confirmation",
    ENROLLMENT_FLOW: "ps-enrollment-confirmation",
}
RETIRED_BINDING_ORDER: Final = 110
USER_LOGIN_MODEL: Final = "authentik_stages_user_login.userloginstage"
REDIRECT_MODEL: Final = "authentik_stages_redirect.redirectstage"
WEBAUTHN_DEVICE_TYPE_SUFFIX: Final = "webauthndevice"
BLUEPRINT_NAME_PREFIX: Final = "PS Service — bundled Authentik setup"
BLUEPRINT_CONFIGMAP_KEY: Final = "authentik-blueprint.yaml"
BLUEPRINT_SUCCESSFUL: Final = "successful"
LOGIN_REPUTATION_POLICY: Final = "ps-login-reputation-policy"
LOCKOUT_SCORE_MARGIN: Final = 5
INVITEE_PREFIX: Final = "live-invitee-"
_INVITEE_PATTERN: Final = re.compile(r"live-invitee-[0-9a-f]{12}")
LOCKOUT_PROBE_PREFIX: Final = "lockout-probe-"
_LOCKOUT_PROBE_PATTERN: Final = re.compile(r"lockout-probe-[0-9a-f]{12}")
LOGIN_FLOW: Final = "ps-login"
PS_CLI_CLIENT_ID: Final = "ps-cli"
CANCEL_ANCHOR_SELECTOR: Final = 'a[href*="/flows/-/cancel/"]'
PASSKEY_PROMPT_SELECTOR: Final = "ak-stage-authenticator-validate"
NOT_YOU_TEXT: Final = "Not you?"
GO_BACK_TEXT: Final = "Go back"
REPUTATION_LOCKOUT_MESSAGE: Final = "Too many failed sign-in attempts"
UNROUTED_PATHS: Final = ("/auth/", "/auth/if/user/", "/auth/if/admin/", "/auth/flows/-/cancel/")

TRANSPORT_ATTEMPTS: Final = 5
WALK_TIMEOUT_MS: Final = 90_000
CANCEL_LINK_WAIT_MS: Final = 10_000
NAVIGATION_COMMIT_TIMEOUT_MS: Final = 15_000
BLUEPRINT_WAIT_SECONDS: Final = 300.0
POLL_INTERVAL_SECONDS: Final = 3.0
PORT_FORWARD_WAIT_SECONDS: Final = 20.0
SUBPROCESS_TIMEOUT_SECONDS: Final = 600.0
EXIT_USAGE: Final = 2
CHROMIUM_INSTALL: Final = "uvx --from playwright==1.63.0 playwright install chromium"

Status = Literal["PASS", "FAIL", "SKIP"]


@dataclass(frozen=True)
class DoneVariant:
    """One request shape sent to the done page: a query string and request headers."""

    label: str
    query: str
    headers: dict[str, str]
    reflected: tuple[str, ...]


@dataclass(frozen=True)
class DoneAnswer:
    """The done page's answer to one variant: status, body bytes and lower-cased headers."""

    label: str
    status: int
    body: bytes
    headers: dict[str, str]


DONE_PAGE_VARIANTS: Final = (
    DoneVariant("plain", "", {}, ()),
    DoneVariant("flow-token", "?flow_token=abc123", {}, ("abc123",)),
    DoneVariant(
        "hostile-query",
        "?next=https%3A%2F%2Fevil.example%2F&q=%3Cscript%3Ealert(1)%3C%2Fscript%3E",
        {},
        ("evil.example", "<script>"),
    ),
    DoneVariant("bearer", "", {"Authorization": "Bearer not-a-token"}, ("not-a-token",)),
    DoneVariant("session-cookie", "", {"Cookie": "authentik_session=x"}, ("authentik_session",)),
)
DONE_PAGE_HEADERS: Final = {
    "content-security-policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}
_ALL_REFLECTION_PROBES: Final = tuple(p for v in DONE_PAGE_VARIANTS for p in v.reflected)


@dataclass(frozen=True)
class CancelAnchor:
    """One link to Authentik's flow cancel path on a page: its target and whether it is visible."""

    href: str
    visible: bool


class CheckFailedError(Exception):
    """A live check found the deployed release in a state the check does not accept."""


@dataclass(frozen=True)
class Outcome:
    """The result of one named check."""

    name: str
    status: Status
    detail: str = ""


@dataclass(frozen=True)
class Options:
    """The parsed command line."""

    host: str
    port: int
    ca: Path | None
    owner_email: str | None
    allow_owner_reset: bool
    first_owner_link: str | None
    only: tuple[str, ...]
    artifacts_dir: Path | None
    namespace: str
    release: str
    authentik_web_path: str
    resolve_to: str | None


if TYPE_CHECKING:
    # playwright is not a workspace dependency (it lives only in this script's uv environment);
    # these Protocols pin the slice of its sync API the script uses.
    class _Locator(Protocol):
        def count(self) -> int: ...
        def inner_text(self) -> str: ...
        def fill(self, value: str) -> None: ...
        def click(self) -> None: ...
        def evaluate_all(self, expression: str) -> object: ...
        def wait_for(self, *, state: str, timeout: float) -> None: ...  # noqa: V107 - protocol parameter names the call contract
        @property
        def first(self) -> _Locator: ...

    class _APIResponse(Protocol):
        @property
        def status(self) -> int: ...
        @property
        def headers(self) -> dict[str, str]: ...
        def body(self) -> bytes: ...
        def text(self) -> str: ...
        def json(self) -> object: ...

    class _APIRequestContext(Protocol):
        def get(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            max_redirects: int = ...,  # noqa: V107 - protocol parameter names the call contract
        ) -> _APIResponse: ...
        def post(
            self,
            url: str,
            *,
            data: dict[str, object] | None = None,  # noqa: V107 - protocol parameter names the call contract
            form: dict[str, str] | None = None,  # noqa: V107 - protocol parameter names the call contract
            headers: dict[str, str] | None = None,
        ) -> _APIResponse: ...
        def delete(self, url: str) -> _APIResponse: ...
        def dispose(self) -> None: ...

    class _CDPSession(Protocol):
        def send(self, method: str, params: dict[str, object] | None = None) -> object: ...

    class _Page(Protocol):
        @property
        def url(self) -> str: ...
        def goto(self, url: str, *, wait_until: str, timeout: float) -> object: ...  # noqa: V107 - protocol parameter names the call contract
        def wait_for_url(self, url: str, *, timeout: float) -> None: ...
        def locator(self, selector: str) -> _Locator: ...  # noqa: V107 - protocol parameter names the call contract
        def screenshot(self, *, path: str, full_page: bool) -> bytes: ...  # noqa: V107 - protocol parameter names the call contract

    class _Context(Protocol):
        @property
        def request(self) -> _APIRequestContext: ...
        def new_page(self) -> _Page: ...
        def new_cdp_session(self, page: _Page) -> _CDPSession: ...
        def close(self) -> None: ...

    class _Browser(Protocol):
        def new_context(self, *, ignore_https_errors: bool) -> _Context: ...  # noqa: V107 - protocol parameter names the call contract
        def close(self) -> None: ...

    class _BrowserType(Protocol):
        def launch(self, *, headless: bool, args: list[str]) -> _Browser: ...  # noqa: V107 - protocol parameter names the call contract

    class _RequestFactory(Protocol):
        def new_context(
            self,
            *,
            base_url: str,
            extra_http_headers: dict[str, str],  # noqa: V107 - protocol parameter names the call contract
            ignore_https_errors: bool,  # noqa: V107 - protocol parameter names the call contract
        ) -> _APIRequestContext: ...

    class _Playwright(Protocol):
        @property
        def chromium(self) -> _BrowserType: ...
        @property
        def request(self) -> _RequestFactory: ...


# --- pure helpers (covered by the fast test) -------------------------------------------------


def parse_args(argv: Sequence[str]) -> Options:
    """Parse the command line into the options of one run.

    Args:
        argv: Command-line arguments without the program name. `--only` takes a comma-separated
            list of names from `CHECK_NAMES`; without it the run selects `DEFAULT_CHECKS`.

    Returns:
        The parsed options. `only` is never empty, and `authentik_web_path` has no trailing slash.

    Raises:
        SystemExit: Exit status 2 (an argparse usage error) when an argument is malformed or
            `--only` names an unknown check or no check at all.
    """
    parser = argparse.ArgumentParser(description="Live browser check of the enrolment pages.")
    parser.add_argument("--host", default="ps.test")
    parser.add_argument("--port", type=int, default=30443)
    parser.add_argument(
        "--ca",
        type=Path,
        default=None,
        help="the eval CA file; give it once the CA is in the system trust store, and TLS is"
        " verified. Without it, certificate errors are ignored.",
    )
    parser.add_argument("--owner-email", default=None)
    parser.add_argument("--allow-owner-reset", action="store_true")
    parser.add_argument("--first-owner-link", default=None)
    parser.add_argument("--only", default=None, help=f"comma-separated: {', '.join(CHECK_NAMES)}")
    parser.add_argument("--artifacts-dir", type=Path, default=None)
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--release", default="policy-system")
    parser.add_argument("--authentik-web-path", default="/auth")
    parser.add_argument(
        "--resolve-to",
        default=None,
        help="connect to this address for --host instead of resolving it (as curl --resolve"
        " does), for both the browser and the direct requests",
    )
    parsed = parser.parse_args(list(argv))
    only = DEFAULT_CHECKS
    if parsed.only is not None:
        only = tuple(name.strip() for name in str(parsed.only).split(",") if name.strip())
        unknown = [name for name in only if name not in CHECK_NAMES]
        if unknown or not only:
            parser.error(f"unknown check name(s) in --only: {', '.join(unknown) or '(none)'}")
    return Options(
        host=str(parsed.host),
        port=int(parsed.port),
        ca=cast("Path | None", parsed.ca),
        owner_email=cast("str | None", parsed.owner_email),
        allow_owner_reset=bool(parsed.allow_owner_reset),
        first_owner_link=cast("str | None", parsed.first_owner_link),
        only=only,
        artifacts_dir=cast("Path | None", parsed.artifacts_dir),
        namespace=str(parsed.namespace),
        release=str(parsed.release),
        authentik_web_path=str(parsed.authentik_web_path).rstrip("/"),
        resolve_to=cast("str | None", parsed.resolve_to),
    )


_HTTPS_LINK = re.compile(r"https://\S+")


def last_https_link(output: str) -> str | None:
    """Find the enrolment link a deploy script prints last in its output.

    Args:
        output: The captured standard output of a deploy script.

    Returns:
        The last `https://` link in `output`, without trailing `.,;)` punctuation, or None when
        `output` holds no `https://` link.
    """
    links = _HTTPS_LINK.findall(output)
    return links[-1].rstrip(".,;)") if links else None


def pinned_request(url: str, options: Options) -> tuple[str, dict[str, str]]:
    """Pin a request for `url` to the `--resolve-to` address.

    A URL on `--host` is sent to the `--resolve-to` address with a `Host` header naming the host
    and port, so the ingress routes it as it routes the host. Any other URL, and every URL when
    `--resolve-to` is not given, is requested as it is.

    Args:
        url: Absolute URL the caller means to request.
        options: The run's options; `host`, `port` and `resolve_to` are read.

    Returns:
        The URL to request and the extra headers to send with it. The headers are empty when the
        URL is requested as it is.
    """
    parts = urlparse(url)
    if options.resolve_to is None or parts.hostname != options.host:
        return url, {}
    port = parts.port or options.port
    pinned = urlunparse(parts._replace(netloc=f"{options.resolve_to}:{port}"))
    return pinned, {"Host": f"{options.host}:{port}"}


def chromium_args(options: Options) -> list[str]:
    """Build the Chromium launch arguments that send `--host` to the `--resolve-to` address.

    Args:
        options: The run's options; `host` and `resolve_to` are read.

    Returns:
        One `--host-resolver-rules` argument, or an empty list when `--resolve-to` is not given.
    """
    if options.resolve_to is None:
        return []
    return [f"--host-resolver-rules=MAP {options.host} {options.resolve_to}"]


def format_outcome(outcome: Outcome) -> str:
    """Format the one output line for a check's outcome.

    Args:
        outcome: The outcome of one check.

    Returns:
        `PASS <name>`, `SKIP <name> (<reason>)` or `FAIL <name>: <detail>`.
    """
    if outcome.status == "PASS":
        return f"PASS {outcome.name}"
    if outcome.status == "SKIP":
        return f"SKIP {outcome.name} ({outcome.detail})"
    return f"FAIL {outcome.name}: {outcome.detail}"


def summary_line(outcomes: Sequence[Outcome]) -> str:
    """Format the final `SUMMARY` line of a run.

    Args:
        outcomes: The outcome of every check the run selected, in run order.

    Returns:
        The pass and fail counts and the names of the skipped checks; `skipped: none` when no
        check was skipped.
    """
    passed = sum(1 for o in outcomes if o.status == "PASS")
    failed = sum(1 for o in outcomes if o.status == "FAIL")
    skipped = [o.name for o in outcomes if o.status == "SKIP"]
    return f"SUMMARY {passed} passed, {failed} failed, skipped: {', '.join(skipped) or 'none'}"


def exit_code(outcomes: Sequence[Outcome]) -> int:
    """Map a run's outcomes to the process exit status.

    Args:
        outcomes: The outcome of every check the run selected. An empty sequence holds no
            failure.

    Returns:
        1 when any outcome is a FAIL, else 0 (also for an empty sequence).
    """
    return 1 if any(o.status == "FAIL" for o in outcomes) else 0


def skip_reason(name: str, options: Options) -> str | None:
    """Decide whether a selected check can run with the given options.

    `first-owner` needs `--first-owner-link`; `owner-reset` needs `--allow-owner-reset`, because it
    revokes the owner's passkeys, and `--owner-email`.

    Args:
        name: A check name from `CHECK_NAMES`.
        options: The run's options.

    Returns:
        Why the check cannot run, or None when it can.
    """
    if name == FIRST_OWNER and options.first_owner_link is None:
        return "no --first-owner-link given"
    if name == OWNER_RESET and not options.allow_owner_reset:
        return "no --allow-owner-reset given; the check revokes the owner's passkeys"
    if name == OWNER_RESET and options.owner_email is None:
        return "no --owner-email given"
    return None


def done_page_problems(path: str, text: str, link_or_button_count: int) -> list[str]:
    """List what is wrong with the final page of an enrolment walk.

    The done page is at `DONE_PATH`, says the passkey is registered and names `ps-cli auth login`,
    never says "denied", and has no link or button.

    Args:
        path: URL path of the final page.
        text: The page's visible text.
        link_or_button_count: How many links and buttons the page holds, zero or more.

    Returns:
        One message per problem; an empty list means the page is the done page.
    """
    problems: list[str] = []
    if path != DONE_PATH:
        problems.append(f"final path is {path!r}, expected {DONE_PATH!r}")
    if "registered" not in text or "ps-cli auth login" not in text:
        problems.append("text does not say the passkey is registered and name ps-cli auth login")
    if "denied" in text.lower():
        problems.append("text contains 'denied'")
    if link_or_button_count:
        problems.append(f"{link_or_button_count} link(s) or button(s) on the page")
    return problems


def invite_link(origin: str, authentik_web_path: str, itoken: str) -> str:
    """Build the invitee-facing link into the invite enrollment flow, as PS Service builds it.

    Args:
        origin: Public origin of the ingress, such as `https://ps.test:30443`, with no trailing
            slash.
        authentik_web_path: Path prefix Authentik is served under, such as `/auth`, with no
            trailing slash.
        itoken: The invitation's primary key; it is URL-quoted into the link.

    Returns:
        The absolute enrollment flow URL carrying the invitation as `?itoken=`.
    """
    return f"{origin}{authentik_web_path}/if/flow/{ENROLLMENT_FLOW}/?itoken={quote(itoken)}"


def new_invitee_username() -> str:
    """Generate a fresh disposable invitee username.

    Returns:
        `live-invitee-` plus 12 random hex digits, a name `is_disposable_invitee` accepts.
    """
    return f"{INVITEE_PREFIX}{secrets.token_hex(6)}"


def is_disposable_invitee(username: str) -> bool:
    """Decide whether a username is one this script generated, and so one it may delete.

    Args:
        username: An Authentik username.

    Returns:
        True only for a name of the form `new_invitee_username` generates.
    """
    return _INVITEE_PATTERN.fullmatch(username) is not None


def probeable_links(hrefs: Sequence[str]) -> list[str]:
    """Pick the links of a page that a direct request can probe.

    Args:
        hrefs: Resolved `href` values of a page's links, in page order.

    Returns:
        The distinct `http` and `https` links among `hrefs`, without their fragment, in
        first-seen order; an empty list when there is none.
    """
    links: list[str] = []
    for href in hrefs:
        if urlparse(href).scheme not in {"http", "https"}:
            continue
        link = href.split("#", 1)[0]
        if link not in links:
            links.append(link)
    return links


def done_page_input_problems(answers: Sequence[DoneAnswer]) -> list[str]:
    """List what is wrong with the done page's answers to the request variants.

    Every answer must be HTTP 200 with the first answer's exact body, contain none of the
    variants' input values, and carry each of `DONE_PAGE_HEADERS` with its exact value.

    Args:
        answers: The done page's answer to each of `DONE_PAGE_VARIANTS`, the plain request first.

    Returns:
        One message per problem, prefixed with the variant's label; an empty list means every
        answer is acceptable, which is also the result for an empty `answers`.
    """
    problems: list[str] = []
    reference = answers[0].body if answers else b""
    for answer in answers:
        if answer.status != 200:  # noqa: PLR2004  # HTTP 200
            problems.append(f"{answer.label}: answered {answer.status}")
        if answer.body != reference:
            problems.append(f"{answer.label}: body differs from the plain request's")
        text = answer.body.decode("utf-8", errors="replace")
        problems.extend(
            f"{answer.label}: body reflects {probe!r}"
            for probe in _ALL_REFLECTION_PROBES
            if probe in text
        )
        problems.extend(
            f"{answer.label}: header {name} is {answer.headers.get(name)!r}"
            for name, value in DONE_PAGE_HEADERS.items()
            if answer.headers.get(name) != value
        )
    return problems


def login_cancel_problems(
    page_url: str, anchors: Sequence[CancelAnchor], not_you_visible: int, *, require_next: bool
) -> list[str]:
    """List what is wrong with a passkey prompt's links to the flow cancel path.

    An attached link proves the prompt rendered the link the brand CSS has to hide. With
    `require_next` (the PS-Cli entry) the page URL and at least one attached link carry `?next=`.

    Args:
        page_url: URL of the page showing the passkey prompt.
        anchors: Every link to the flow cancel path on the page, visible or not.
        not_you_visible: How many visible elements show `NOT_YOU_TEXT`, zero or more.
        require_next: Whether the login was entered from PS-Cli's device authorization, so the
            `?next=` query must survive onto the page and a cancel link.

    Returns:
        One message per problem; an empty list means a cancel link is attached and every one is
        hidden. No attached link is itself a problem.
    """
    problems: list[str] = []
    if require_next and "next=" not in urlparse(page_url).query:
        problems.append(f"page URL {page_url!r} has no ?next= query")
    if not anchors:
        problems.append("no cancel link is attached to the passkey prompt")
    elif require_next and not any("?next=" in a.href for a in anchors):
        problems.append("no attached cancel link carries ?next=")
    visible = sum(1 for a in anchors if a.visible)
    if visible:
        problems.append(f"{visible} visible cancel link(s)")
    if not_you_visible:
        problems.append(f"{not_you_visible} visible {NOT_YOU_TEXT!r} element(s)")
    return problems


def lockout_page_problems(
    *, message_visible: bool, anchors: Sequence[CancelAnchor], go_back_visible: int
) -> list[str]:
    """List what is wrong with the reputation lockout page.

    Args:
        message_visible: Whether `REPUTATION_LOCKOUT_MESSAGE` is visible on the page.
        anchors: Every link to the flow cancel path on the page, visible or not.
        go_back_visible: How many visible elements show `GO_BACK_TEXT`, zero or more.

    Returns:
        One message per problem; an empty list means the message shows and no cancel link or
        "Go back" element does.
    """
    problems: list[str] = []
    if not message_visible:
        problems.append(f"the lockout message {REPUTATION_LOCKOUT_MESSAGE!r} is not visible")
    visible = sum(1 for a in anchors if a.visible)
    if visible:
        problems.append(f"{visible} visible cancel link(s)")
    if go_back_visible:
        problems.append(f"{go_back_visible} visible {GO_BACK_TEXT!r} element(s)")
    return problems


def new_lockout_probe_username() -> str:
    """Generate a fresh disposable lockout probe username.

    Returns:
        `lockout-probe-` plus 12 random hex digits, a name `is_lockout_probe` accepts.
    """
    return f"{LOCKOUT_PROBE_PREFIX}{secrets.token_hex(6)}"


def is_lockout_probe(username: str) -> bool:
    """Decide whether a username is a lockout probe this script generated, and so one it may delete.

    Args:
        username: An Authentik username.

    Returns:
        True only for a name of the form `new_lockout_probe_username` generates.
    """
    return _LOCKOUT_PROBE_PATTERN.fullmatch(username) is not None


def lockout_seed_score(
    policy: dict[str, object], scores: Sequence[dict[str, object]], probe: str
) -> int:
    """Compute the score to seed for a probe so the login reputation policy locks it out.

    Authentik's reputation policy sums every score whose IP is the client's (when it checks the
    IP) or whose identifier is the username (when it checks the username), and fires when that
    sum is at or below its threshold. The client IP is not known before the login, and every
    client of a kind cluster reaches Authentik from one pod-network address, so the seed cancels
    the highest positive total that any one IP in `scores` (with the probe's own rows) holds and
    lands `LOCKOUT_SCORE_MARGIN` below the threshold.

    Args:
        policy: The login reputation policy as the Authentik admin API returns it; `check_ip`,
            `check_username` and `threshold` are read.
        scores: Every reputation score row as the admin API returns it; empty when none exists.
        probe: The lockout probe's username.

    Returns:
        The score to seed, at most `threshold - LOCKOUT_SCORE_MARGIN`; with no matching row it
        is exactly that.

    Raises:
        ValueError: The policy does not check the username, so no per-probe seed can fire it.
    """
    if not policy.get("check_username"):
        message = "the login reputation policy does not check the username"
        raise ValueError(message)
    check_ip = bool(policy.get("check_ip"))
    threshold = int(cast("int", policy["threshold"]))

    def matched_total(ip: str | None) -> int:
        return sum(
            int(cast("int", row.get("score", 0)))
            for row in scores
            if row.get("identifier") == probe or (ip is not None and row.get("ip") == ip)
        )

    client_ips = {str(row.get("ip")) for row in scores} if check_ip else set[str]()
    current = max([matched_total(None), *(matched_total(ip) for ip in client_ips)])
    return threshold - LOCKOUT_SCORE_MARGIN - max(current, 0)


def reputation_shell_argv(options: Options, probe: str, *, score: int | None) -> list[str]:
    """Build the `kubectl exec ... ak shell -c` argv that seeds a probe's score or deletes its rows.

    With `score` it creates one reputation row for the probe's username; with None it deletes the
    rows whose identifier is the probe's username, which only this check's probe login creates.
    The Python code is fixed apart from the generated probe name, which is checked first, and an
    integer score, both passed inside one argv element, never through a shell. Building the argv
    runs nothing.

    Args:
        options: The run's options; `namespace` and `release` name the Authentik worker.
        probe: A lockout probe username from `new_lockout_probe_username`.
        score: The score to seed, or None to delete the probe's rows.

    Returns:
        The full argv, ready for a subprocess call without a shell.

    Raises:
        ValueError: `probe` is not a generated lockout probe name.
    """
    if not is_lockout_probe(probe):
        message = f"{probe!r} is not a generated lockout probe name"
        raise ValueError(message)
    action = (
        f"Reputation.objects.filter(identifier={probe!r}).delete()"
        if score is None
        else f"Reputation.objects.create(identifier={probe!r}, ip='0.0.0.0', score={int(score)})"
    )
    code = f"from authentik.policies.reputation.models import Reputation; {action}"
    return [
        "kubectl",
        "-n",
        options.namespace,
        "exec",
        f"deploy/{options.release}-authentik-worker",
        "--",
        "ak",
        "shell",
        "-c",
        code,
    ]


def blueprint_content_hash(content: str) -> str:
    """Compute the digest Authentik records as a blueprint's `last_applied_hash`.

    Args:
        content: The blueprint's text.

    Returns:
        The SHA-512 hex digest of the UTF-8 encoded text.
    """
    return hashlib.sha512(content.encode("utf-8")).hexdigest()


def blueprint_pending_reason(instances: Sequence[dict[str, object]], deployed: str) -> str | None:
    """Explain why the bundled blueprint is not yet the applied deployed text.

    The applied content is the deployed one when Authentik's `last_applied_hash` equals the hash of
    the deployed text, and the apply counts only when its status is successful. An unchanged
    blueprint is not re-applied on a redeploy, so the apply time says nothing about the content.

    Args:
        instances: Every blueprint instance the Authentik admin API lists; empty when none.
        deployed: The bundled blueprint text in the release's ConfigMap.

    Returns:
        Why the bundled blueprint is not yet applied (also when no instance is the bundled one),
        or None when the deployed text is applied successfully.
    """
    bundled = [i for i in instances if str(i.get("name", "")).startswith(BLUEPRINT_NAME_PREFIX)]
    if not bundled:
        return "the bundled blueprint is not found"
    status = str(bundled[0].get("status"))
    if bundled[0].get("last_applied_hash") != blueprint_content_hash(deployed):
        return f"the applied bundled blueprint is not the deployed one (status {status})"
    if status != BLUEPRINT_SUCCESSFUL:
        return f"the deployed bundled blueprint applied with status {status}"
    return None


def count_webauthn_devices(devices: object) -> int:
    """Count the WebAuthn devices in an all-authenticators admin list.

    Authentik names a device's model in `type` as `<app>.<ModelClass>`
    (`authentik_stages_authenticator_webauthn.WebAuthnDevice`), so the match ignores case.

    Args:
        devices: The parsed JSON body of the all-authenticators admin view, a list of rows.

    Returns:
        How many rows are WebAuthn devices; 0 when there is none or `devices` is not a list.
    """
    rows = cast("list[object]", devices) if isinstance(devices, list) else []
    return sum(
        1
        for row in rows
        if isinstance(row, dict)
        and str(cast("dict[str, object]", row).get("type", ""))
        .lower()
        .endswith(WEBAUTHN_DEVICE_TYPE_SUFFIX)
    )


_NAVIGATION_TRANSPORT_FAILURE: Final = re.compile(
    r"net::ERR_(?:TIMED_OUT|CONNECTION_\w+|EMPTY_RESPONSE|SOCKET_NOT_CONNECTED)\b"
    r"|Timeout \d+ms exceeded"
)


def is_navigation_transport_failure(message: str) -> bool:
    """Decide whether a navigation error means no response arrived.

    A dropped or stalled connection is one. A certificate, name resolution or page error is not:
    it would fail the same way again.

    Args:
        message: The text of the error a navigation raised.

    Returns:
        True when the error names a connection failure or a navigation timeout.
    """
    return _NAVIGATION_TRANSPORT_FAILURE.search(message) is not None


def _json_list(value: object, key: str = "results") -> list[dict[str, object]]:
    if isinstance(value, dict):
        inner = cast("dict[str, object]", value).get(key)
        if isinstance(inner, list):
            return [
                cast("dict[str, object]", item)
                for item in cast("list[object]", inner)
                if isinstance(item, dict)
            ]
    return []


def all_pages(fetch: Callable[[int], object]) -> list[dict[str, object]]:
    """Read every result of a paginated Authentik API list.

    Authentik's list views answer one page at a time, with the next page's number in
    `pagination.next` (0 on the last page).

    Args:
        fetch: Returns the parsed JSON body of the given page number, starting at 1.

    Returns:
        The `results` rows of every page in page order; an empty list when the list is empty or
        a body has no `results` list.

    Side Effects:
        Calls `fetch` once per page.
    """
    results: list[dict[str, object]] = []
    page = 1
    while page:
        body = fetch(page)
        results.extend(_json_list(body))
        pagination = (
            cast("dict[str, object]", body).get("pagination") if isinstance(body, dict) else None
        )
        page = (
            int(cast("int", cast("dict[str, object]", pagination).get("next") or 0))
            if isinstance(pagination, dict)
            else 0
        )
    return results


# --- live environment --------------------------------------------------------------------------


@dataclass
class LiveEnv:
    """The browser, the ingress origin and the Authentik admin API of one run."""

    options: Options
    playwright: _Playwright
    browser: _Browser
    authentik: _APIRequestContext
    ingress: _APIRequestContext
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def origin(self) -> str:
        """The public origin of the ingress, `https://<host>:<port>`."""
        return f"https://{self.options.host}:{self.options.port}"

    def ingress_get(self, path: str) -> _APIResponse:
        """GET a path through the ingress without following redirects.

        Args:
            path: Absolute path on the ingress origin, with any query string.

        Returns:
            The response, whatever its HTTP status.

        Raises:
            Exception: The transport error of the last attempt, when every attempt fails.

        Side Effects:
            Sends up to `TRANSPORT_ATTEMPTS` HTTP requests, retrying only transport errors.
        """
        return self.ingress_request(self.origin + path)

    def ingress_request(self, url: str, headers: dict[str, str] | None = None) -> _APIResponse:
        """GET a URL through the ingress without following redirects.

        The request goes to the `--resolve-to` address when one is given.

        Args:
            url: Absolute URL to request.
            headers: Extra request headers, or None for none.

        Returns:
            The response, whatever its HTTP status.

        Raises:
            Exception: The transport error of the last attempt, when every attempt fails.

        Side Effects:
            Sends up to `TRANSPORT_ATTEMPTS` HTTP requests, retrying only transport errors.
        """
        target, pin_headers = pinned_request(url, self.options)
        sent = {**(headers or {}), **pin_headers}
        return _with_transport_retry(
            lambda: self.ingress.get(target, headers=sent, max_redirects=0)
        )

    def api(self, path: str) -> object:
        """GET a path under the Authentik admin API.

        Args:
            path: Path below `api/v3`, starting with `/`, with any query string.

        Returns:
            The parsed JSON body.

        Raises:
            CheckFailedError: The API answers a status other than 200.

        Side Effects:
            Sends one HTTP request through the port-forward to Authentik.
        """
        response = self.authentik.get(f"api/v3{path}")
        if response.status != 200:  # noqa: PLR2004  # HTTP 200
            message = f"Authentik API GET {path} answered {response.status}"
            raise CheckFailedError(message)
        return response.json()

    def api_post(self, path: str, body: dict[str, object]) -> object:
        """POST a JSON body to a path under the Authentik admin API.

        Args:
            path: Path below `api/v3`, starting with `/`.
            body: The object to create, sent as JSON.

        Returns:
            The parsed JSON body of the created object.

        Raises:
            CheckFailedError: The API answers a status other than 201.

        Side Effects:
            Creates one object in Authentik.
        """
        response = self.authentik.post(f"api/v3{path}", data=body)
        if response.status != 201:  # noqa: PLR2004  # HTTP 201
            message = f"Authentik API POST {path} answered {response.status}"
            raise CheckFailedError(message)
        return response.json()

    def api_delete(self, path: str) -> int:
        """DELETE a path under the Authentik admin API.

        Args:
            path: Path below `api/v3`, starting with `/`.

        Returns:
            The HTTP status; the caller decides whether it is acceptable.

        Side Effects:
            Deletes the object at `path` in Authentik, when it exists.
        """
        return self.authentik.delete(f"api/v3{path}").status


def _with_transport_retry(call: Callable[[], _APIResponse]) -> _APIResponse:
    """Run `call`, retrying only when it raises (a transport error), never on an HTTP status."""
    for attempt in range(1, TRANSPORT_ATTEMPTS + 1):
        try:
            return call()
        except Exception:  # playwright raises its own Error type for a transport failure
            if attempt == TRANSPORT_ATTEMPTS:
                raise
            time.sleep(1.0)
    message = "unreachable"
    raise AssertionError(message)


def _run(argv: list[str], *, timeout: float = SUBPROCESS_TIMEOUT_SECONDS) -> str:
    completed = subprocess.run(  # noqa: S603  # fixed argv built from literals and parsed options
        argv, capture_output=True, text=True, check=False, timeout=timeout
    )
    if completed.returncode != 0:
        message = f"{argv[0]} {argv[1]} exited {completed.returncode}: {completed.stderr[-500:]}"
        raise CheckFailedError(message)
    return completed.stdout


def _read_api_token(options: Options) -> str:
    import base64  # noqa: PLC0415  # only the live path decodes the Secret

    encoded = _run(
        [
            "kubectl",
            "-n",
            options.namespace,
            "get",
            "secret",
            f"{options.release}-authentik-api-token",
            "-o",
            "jsonpath={.data.PS_AUTHENTIK_API_TOKEN}",
        ]
    )
    return base64.b64decode(encoded).decode("utf-8")


@contextlib.contextmanager
def _authentik_port_forward(options: Options) -> Generator[int]:
    """A `kubectl port-forward` to the Authentik server Service; yields the local port.

    Its output goes to a temporary file, not a pipe, so the per-connection lines kubectl keeps
    writing can never fill a pipe buffer and stall the forward.
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as log:
        process = subprocess.Popen(  # noqa: S603  # fixed argv
            [  # noqa: S607  # kubectl from PATH, as every deploy script runs it
                "kubectl",
                "-n",
                options.namespace,
                "port-forward",
                f"svc/{options.release}-authentik-server",
                ":80",
                "--address",
                "127.0.0.1",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            yield _forwarded_port(process, log)
        finally:
            process.terminate()
            process.wait(timeout=10)


def _forwarded_port(process: subprocess.Popen[bytes], log: IO[str]) -> int:
    deadline = time.monotonic() + PORT_FORWARD_WAIT_SECONDS
    while time.monotonic() < deadline:
        _ = log.seek(0)
        match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", log.read())
        if match is not None:
            return int(match.group(1))
        if process.poll() is not None:
            break
        time.sleep(0.2)
    _ = log.seek(0)
    message = f"kubectl port-forward did not start: {log.read().strip()[-300:]}"
    raise CheckFailedError(message)


# --- browser steps -------------------------------------------------------------------------------


def _add_virtual_passkey(context: _Context, page: _Page) -> None:
    session = context.new_cdp_session(page)
    session.send("WebAuthn.enable")
    session.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": "internal",
                "hasResidentKey": True,
                "hasUserVerification": True,
                "isUserVerified": True,
                "automaticPresenceSimulation": True,
            }
        },
    )


_VISIBLE_HREFS_JS: Final = (
    "anchors => anchors.filter(a => a.getClientRects().length > 0).map(a => a.href)"
)


def _visible_404_links(env: LiveEnv, page: _Page) -> list[str]:
    hrefs = cast("list[str]", page.locator("a[href]").evaluate_all(_VISIBLE_HREFS_JS))
    return [
        link
        for link in probeable_links(hrefs)
        if env.ingress_request(link).status == 404  # noqa: PLR2004  # HTTP 404
    ]


def _save_page_artifacts(env: LiveEnv, page: _Page, label: str) -> str:
    """Save a screenshot and the visible text of `page` under `--artifacts-dir`; return the text."""
    text = page.locator("body").inner_text()
    if env.options.artifacts_dir is not None:
        env.options.artifacts_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(env.options.artifacts_dir / f"{label}.png"), full_page=True)
        (env.options.artifacts_dir / f"{label}.txt").write_text(text, encoding="utf-8")
    return text


# Playwright's CSS engine pierces open shadow roots, so these reach the links inside Authentik's
# page components. An element hidden by `display: none` (its own or an ancestor's) has no client
# rects.
_ANCHOR_STATES_JS: Final = (
    "anchors => anchors.map(a => [a.getAttribute('href') || '', a.getClientRects().length > 0])"
)
_VISIBLE_COUNT_JS: Final = "nodes => nodes.filter(n => n.getClientRects().length > 0).length"


def _cancel_anchors(page: _Page) -> list[CancelAnchor]:
    states = cast(
        "list[list[object]]", page.locator(CANCEL_ANCHOR_SELECTOR).evaluate_all(_ANCHOR_STATES_JS)
    )
    return [CancelAnchor(str(href), visible=bool(visible)) for href, visible in states]


def _visible_text_count(page: _Page, text: str) -> int:
    return int(cast("int", page.locator(f"text={text}").evaluate_all(_VISIBLE_COUNT_JS)))


def _identify_on_login(page: _Page, username: str) -> None:
    page.locator('input[name="uidField"]').fill(username)
    page.locator('button[type="submit"]').click()


def _goto(page: _Page, url: str) -> None:
    """Navigate to `url`, retrying only when no response arrived (see `_with_transport_retry`).

    The navigation returns once the response starts, so a retry never repeats a navigation
    the server answered.
    """
    for attempt in range(1, TRANSPORT_ATTEMPTS + 1):
        try:
            _ = page.goto(url, wait_until="commit", timeout=NAVIGATION_COMMIT_TIMEOUT_MS)
        except Exception as error:  # playwright raises its own Error type for a navigation failure
            if attempt == TRANSPORT_ATTEMPTS or not is_navigation_transport_failure(str(error)):
                raise
            time.sleep(1.0)
        else:
            return


def walk_to_done_page(
    env: LiveEnv, link: str, label: str, before_passkey: Callable[[_Page], None] | None = None
) -> None:
    """Walk an enrolment link with a new virtual passkey and require that it ends on the done page.

    The final page must also have no visible link that answers 404, and the done page must answer
    200 through the ingress.

    Args:
        env: The run's live environment.
        link: The enrolment link to open.
        label: Name for the saved page artifacts and the failure messages.
        before_passkey: Completes the flow's steps before the passkey registration (the invite
            flow's prompt), or None when the flow starts with the registration.

    Raises:
        CheckFailedError: The walk never reaches the done page, or the done page has a problem.

    Side Effects:
        Opens and closes a browser context, registers a virtual passkey on the walked account,
        which leaves that account logged in, sends direct requests through the ingress, and saves
        a screenshot and the page text under `--artifacts-dir` when it is given.
    """
    context = env.browser.new_context(ignore_https_errors=env.options.ca is None)
    try:
        page = context.new_page()
        _add_virtual_passkey(context, page)
        _goto(page, link)
        if before_passkey is not None:
            before_passkey(page)
        try:
            page.wait_for_url(f"**{DONE_PATH}", timeout=WALK_TIMEOUT_MS)
        except (
            Exception
        ) as error:  # playwright's timeout error; the page it stopped on is the evidence
            stopped = _save_page_artifacts(env, page, f"{label}-failed")
            message = (
                f"{label}: never reached {DONE_PATH}; stopped on {urlparse(page.url).path!r}"
                f" showing {' '.join(stopped.split())[:200]!r}"
            )
            raise CheckFailedError(message) from error
        text = _save_page_artifacts(env, page, label)
        problems = done_page_problems(
            urlparse(page.url).path, text, page.locator("a, button, [role=button]").count()
        )
        problems.extend(
            f"visible link {dead} answers 404" for dead in _visible_404_links(env, page)
        )
        status = env.ingress_get(DONE_PATH).status
        if status != 200:  # noqa: PLR2004  # HTTP 200
            problems.append(f"GET {DONE_PATH} through the ingress answered {status}")
        if problems:
            raise CheckFailedError(f"{label}: " + "; ".join(problems))
    finally:
        context.close()


# --- Authentik admin API reads -------------------------------------------------------------------


def _user_pk(env: LiveEnv, username: str) -> int:
    users = _json_list(env.api(f"/core/users/?username={quote(username)}"))
    if len(users) != 1:
        message = f"expected one Authentik user {username!r}, found {len(users)}"
        raise CheckFailedError(message)
    return int(cast("int", users[0]["pk"]))


def webauthn_device_count(env: LiveEnv, user_pk: int) -> int:
    """Count a user's WebAuthn devices, read from the all-authenticators admin view.

    Args:
        env: The run's live environment.
        user_pk: The Authentik user's primary key.

    Returns:
        How many WebAuthn devices the user holds; 0 when none.

    Raises:
        CheckFailedError: The admin API answers a status other than 200.
    """
    return count_webauthn_devices(env.api(f"/authenticators/admin/all/?user={user_pk}"))


def _events_after(env: LiveEnv, action: str, username: str, after: datetime) -> list[datetime]:
    events = _json_list(
        env.api(f"/events/events/?action={action}&username={quote(username)}&ordering=-created")
    )
    created = [datetime.fromisoformat(str(e["created"])) for e in events]
    return [c for c in created if c > after]


def require_login_without_logout(env: LiveEnv, username: str, after: datetime) -> None:
    """Require that a user logged in after a time and has not logged out since.

    Args:
        env: The run's live environment.
        username: The Authentik username.
        after: The time the walk started; only events after it count.

    Raises:
        CheckFailedError: No `login` event follows `after`, a `logout` event follows the first such
            login, or the admin API answers a status other than 200.
    """
    logins = _events_after(env, "login", username, after)
    if not logins:
        message = f"no login event for {username} after the walk started"
        raise CheckFailedError(message)
    if _events_after(env, "logout", username, min(logins)):
        message = f"a logout event for {username} follows the enrolment login"
        raise CheckFailedError(message)


def _flow_bindings(env: LiveEnv, slug: str) -> list[dict[str, object]]:
    flow = cast("dict[str, object]", env.api(f"/flows/instances/{slug}/"))
    bindings = _json_list(env.api(f"/flows/bindings/?target={flow['pk']}&ordering=order"))
    return sorted(bindings, key=lambda b: int(cast("int", b["order"])))


def _stage_model(binding: dict[str, object]) -> str:
    stage = cast("dict[str, object]", binding.get("stage_obj", {}))
    return str(stage.get("meta_model_name", ""))


def _stage_name(binding: dict[str, object]) -> str:
    stage = cast("dict[str, object]", binding.get("stage_obj", {}))
    return str(stage.get("name", ""))


def _deployed_blueprint(options: Options) -> str:
    """The bundled blueprint text in the release's ConfigMap, the file Authentik mounts."""
    configmap = json.loads(
        _run(
            [
                "kubectl",
                "-n",
                options.namespace,
                "get",
                "configmap",
                f"{options.release}-authentik-blueprint",
                "-o",
                "json",
            ]
        )
    )
    data = cast("dict[str, object]", cast("dict[str, object]", configmap).get("data", {}))
    if BLUEPRINT_CONFIGMAP_KEY not in data:
        message = f"the blueprint ConfigMap has no {BLUEPRINT_CONFIGMAP_KEY} key"
        raise CheckFailedError(message)
    return str(data[BLUEPRINT_CONFIGMAP_KEY])


def _wait_for_deployed_blueprint_applied(env: LiveEnv) -> None:
    """Wait a bounded time until Authentik has successfully applied the deployed blueprint text."""
    deployed = _deployed_blueprint(env.options)
    deadline = time.monotonic() + BLUEPRINT_WAIT_SECONDS
    while True:
        instances = all_pages(lambda page: env.api(f"/managed/blueprints/?page={page}"))
        reason = blueprint_pending_reason(instances, deployed)
        if reason is None:
            return
        if time.monotonic() >= deadline:
            message = f"{reason} after {BLUEPRINT_WAIT_SECONDS:.0f} s"
            raise CheckFailedError(message)
        time.sleep(POLL_INTERVAL_SECONDS)


# --- checks --------------------------------------------------------------------------------------


def check_upgrade_precondition(env: LiveEnv) -> None:
    """Require the state of a release before the upgrade under test.

    Both enrolment flows bind their deny confirmation stage (`RETIRED_DENY_STAGES`) at order
    `RETIRED_BINDING_ORDER`. Reads the Authentik admin API only.

    Args:
        env: The run's live environment.

    Raises:
        CheckFailedError: A flow does not bind its deny stage at that order exactly once, or the
            admin API answers a status other than 200.
    """
    for slug, stage_name in RETIRED_DENY_STAGES.items():
        bound = [
            b
            for b in _flow_bindings(env, slug)
            if int(cast("int", b["order"])) == RETIRED_BINDING_ORDER
            and _stage_name(b) == stage_name
        ]
        if len(bound) != 1:
            message = f"{slug} does not bind {stage_name} at order {RETIRED_BINDING_ORDER}"
            raise CheckFailedError(message)


def check_upgrade_state(env: LiveEnv, slug: str) -> None:
    """Require that an enrolment flow ends on one static redirect to the done page.

    Waits a bounded time for Authentik to apply the deployed blueprint first. After user login the
    flow has exactly one binding, a static redirect stage to `DONE_PATH`, and the flow's deny
    confirmation stage does not exist.

    Args:
        env: The run's live environment.
        slug: The flow's slug, `RECOVERY_FLOW` or `ENROLLMENT_FLOW`.

    Raises:
        CheckFailedError: The deployed blueprint is not applied within `BLUEPRINT_WAIT_SECONDS`,
            the flow's bindings after user login are not one static redirect to the done page, the
            deny stage exists, or a kubectl or admin API call fails.

    Side Effects:
        Reads the blueprint ConfigMap with kubectl and polls the Authentik admin API.
    """
    _wait_for_deployed_blueprint_applied(env)
    bindings = _flow_bindings(env, slug)
    login_order = next(
        int(cast("int", b["order"])) for b in bindings if _stage_model(b) == USER_LOGIN_MODEL
    )
    after_login = [b for b in bindings if int(cast("int", b["order"])) > login_order]
    if len(after_login) != 1 or _stage_model(after_login[0]) != REDIRECT_MODEL:
        message = f"{slug} has {len(after_login)} binding(s) after user login, not one redirect"
        raise CheckFailedError(message)
    stage = cast("dict[str, object]", env.api(f"/stages/redirect/{after_login[0]['stage']}/"))
    if stage.get("mode") != "static" or stage.get("target_static") != DONE_PATH:
        message = f"{slug} redirect stage is not a static redirect to {DONE_PATH}"
        raise CheckFailedError(message)
    retired = RETIRED_DENY_STAGES[slug]
    if _json_list(env.api(f"/stages/deny/?name={quote(retired)}")):
        message = f"the retired deny stage {retired} still exists"
        raise CheckFailedError(message)


def check_first_owner(env: LiveEnv) -> None:
    """Walk the first-owner enrolment link to the done page.

    With `--owner-email`, the owner must then hold a WebAuthn device and be logged in.

    Args:
        env: The run's live environment; `--first-owner-link` must be given.

    Raises:
        CheckFailedError: The walk fails, the owner holds no WebAuthn device, the owner has no
            login after the walk or a logout follows it, or an admin API call fails.

    Side Effects:
        Redeems the single-use first-owner link and registers a virtual passkey for the owner.
    """
    link = cast("str", env.options.first_owner_link)
    started = datetime.now(UTC)
    walk_to_done_page(env, link, "first-owner")
    if env.options.owner_email is not None:
        owner_pk = _user_pk(env, env.options.owner_email)
        if webauthn_device_count(env, owner_pk) < 1:
            message = "the owner holds no WebAuthn device after the first-owner walk"
            raise CheckFailedError(message)
        require_login_without_logout(env, env.options.owner_email, started)


def _reset_owner_link(env: LiveEnv) -> str:
    output = _run(
        [
            str(DEPLOY_EVAL_SCRIPT),
            "--reset-owner-passkey",
            "--owner-email",
            cast("str", env.options.owner_email),
            "--yes",
        ]
    )
    link = last_https_link(output)
    if link is None:
        message = "deploy-ps-eval.sh --reset-owner-passkey printed no https link"
        raise CheckFailedError(message)
    return link


def _require_device_count(
    env: LiveEnv, owner_pk: int, accept: Callable[[int], bool], when: str
) -> None:
    count = webauthn_device_count(env, owner_pk)
    if not accept(count):
        message = f"the owner holds {count} WebAuthn device(s) {when}"
        raise CheckFailedError(message)


def check_owner_reset(env: LiveEnv) -> None:
    """Reset and re-enrol the owner's passkey twice, each walk ending on the done page.

    The owner holds at least one WebAuthn device after the first walk, none after the second
    reset, and exactly one after the second walk, and is logged in after it.

    Args:
        env: The run's live environment; `--owner-email` and `--allow-owner-reset` must be given.

    Raises:
        CheckFailedError: A reset prints no link, a walk fails, a device count is off, the login
            check fails, or a subprocess or admin API call fails.

    Side Effects:
        Runs `deploy-ps-eval.sh --reset-owner-passkey` twice, which revokes every passkey the
        owner holds, and leaves the owner with one virtual passkey that no real device holds.
    """
    email = cast("str", env.options.owner_email)
    owner_pk = _user_pk(env, email)
    walk_to_done_page(env, _reset_owner_link(env), "owner-reset-a")
    _require_device_count(env, owner_pk, lambda n: n >= 1, "after the first walk")
    second_link = _reset_owner_link(env)
    _require_device_count(env, owner_pk, lambda n: n == 0, "after the second reset")
    started = datetime.now(UTC)
    walk_to_done_page(env, second_link, "owner-reset-b")
    _require_device_count(env, owner_pk, lambda n: n == 1, "after the second walk")
    require_login_without_logout(env, email, started)


def _fill_invite_prompt(username: str) -> Callable[[_Page], None]:
    def fill(page: _Page) -> None:
        page.locator('input[name="username"]').fill(username)
        page.locator('input[name="name"]').fill("Live Invitee")
        page.locator('button[type="submit"]').click()

    return fill


def _delete_invitee(env: LiveEnv, username: str) -> None:
    if not is_disposable_invitee(username):
        message = f"refusing to delete {username!r}: not a generated invitee"
        raise CheckFailedError(message)
    for user in _json_list(env.api(f"/core/users/?username={quote(username)}")):
        _ = env.api_delete(f"/core/users/{user['pk']}/")


@contextlib.contextmanager
def _enrolled_invitee(env: LiveEnv, label: str) -> Generator[tuple[str, datetime]]:
    """Redeem a fresh invitation with a virtual passkey to the done page.

    Yields the invitee's username and the time the walk started. The invitation and the invitee
    are deleted on exit, even after a failure.
    """
    username = new_invitee_username()
    invitation = cast(
        "dict[str, object]",
        env.api_post(
            "/stages/invitation/invitations/",
            {
                "name": username,
                "single_use": True,
                "fixed_data": {"email": f"{username}@example.invalid"},
                "expires": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
            },
        ),
    )
    itoken = str(invitation["pk"])
    try:
        started = datetime.now(UTC)
        link = invite_link(env.origin, env.options.authentik_web_path, itoken)
        walk_to_done_page(env, link, label, _fill_invite_prompt(username))
        yield username, started
    finally:
        _ = env.api_delete(f"/stages/invitation/invitations/{quote(itoken)}/")
        _delete_invitee(env, username)


def check_invite(env: LiveEnv) -> None:
    """Redeem a fresh invitation with a virtual passkey; it ends on the done page, logged in.

    Args:
        env: The run's live environment.

    Raises:
        CheckFailedError: The walk fails, the invitee holds no WebAuthn device, the login check
            fails, or an admin API call fails.

    Side Effects:
        Creates a single-use invitation and a disposable invitee, and deletes both on exit, also
        after a failure.
    """
    with _enrolled_invitee(env, "invite") as (username, started):
        if webauthn_device_count(env, _user_pk(env, username)) < 1:
            message = "the invitee holds no WebAuthn device after the walk"
            raise CheckFailedError(message)
        require_login_without_logout(env, username, started)


def check_done_page_inputs(env: LiveEnv) -> None:
    """Require that the done page answers every request variant alike, with its security headers.

    Args:
        env: The run's live environment.

    Raises:
        CheckFailedError: An answer is not 200, differs from the plain request's, reflects an
            input value, or lacks a security header value.

    Side Effects:
        Sends one request per variant of `DONE_PAGE_VARIANTS` through the ingress.
    """
    answers: list[DoneAnswer] = []
    for variant in DONE_PAGE_VARIANTS:
        url = f"{env.origin}{DONE_PATH}{variant.query}"
        response = env.ingress_request(url, variant.headers)
        headers = {name.lower(): value for name, value in response.headers.items()}
        answers.append(DoneAnswer(variant.label, response.status, response.body(), headers))
    problems = done_page_input_problems(answers)
    if problems:
        raise CheckFailedError("; ".join(problems))


def _login_url(env: LiveEnv) -> str:
    return f"{env.origin}{env.options.authentik_web_path}/if/flow/{LOGIN_FLOW}/"


def _ps_cli_login_entry(env: LiveEnv) -> str:
    """Start a device authorization as PS-Cli does; return its `verification_uri_complete`."""
    url, headers = pinned_request(
        f"{env.origin}{env.options.authentik_web_path}/application/o/device/", env.options
    )
    response = _with_transport_retry(
        lambda: env.ingress.post(
            url, form={"client_id": PS_CLI_CLIENT_ID, "scope": "openid"}, headers=headers
        )
    )
    if response.status != 200:  # noqa: PLR2004  # HTTP 200
        message = f"device authorization for {PS_CLI_CLIENT_ID} answered {response.status}"
        raise CheckFailedError(message)
    body = cast("dict[str, object]", response.json())
    return str(body["verification_uri_complete"])


def _login_prompt_problems(
    env: LiveEnv, entry: str, username: str, label: str, *, require_next: bool
) -> list[str]:
    """Open `entry` in a fresh browser, identify as `username` and inspect the passkey prompt."""
    context = env.browser.new_context(ignore_https_errors=env.options.ca is None)
    try:
        page = context.new_page()
        _goto(page, entry)
        page.wait_for_url(f"**/if/flow/{LOGIN_FLOW}/**", timeout=WALK_TIMEOUT_MS)
        _identify_on_login(page, username)
        page.locator(PASSKEY_PROMPT_SELECTOR).first.wait_for(
            state="attached", timeout=WALK_TIMEOUT_MS
        )
        # A link that never attaches is reported by login_cancel_problems, with a clearer reason
        # than the wait's timeout.
        with contextlib.suppress(Exception):
            page.locator(CANCEL_ANCHOR_SELECTOR).first.wait_for(
                state="attached", timeout=CANCEL_LINK_WAIT_MS
            )
        _ = _save_page_artifacts(env, page, label)
        return login_cancel_problems(
            page.url,
            _cancel_anchors(page),
            _visible_text_count(page, NOT_YOU_TEXT),
            require_next=require_next,
        )
    finally:
        context.close()


def check_login_cancel_hidden(env: LiveEnv) -> None:
    """Require that the passkey prompt attaches its cancel link but shows none.

    Both ways into the login are checked: PS-Cli's device authorization and the login flow URL.

    Args:
        env: The run's live environment.

    Raises:
        CheckFailedError: A prompt has no attached cancel link, a visible one, a visible "Not you?"
            element, or (from PS-Cli) loses `?next=`, or an admin API or device authorization call
            fails.

    Side Effects:
        Creates and enrols a disposable invitee (deleted with its invitation on exit), starts one
        PS-Cli device authorization, and saves page artifacts under `--artifacts-dir` when given.
    """
    problems: list[str] = []
    with _enrolled_invitee(env, "login-cancel-invitee") as (username, _):
        entries = (
            ("login-cancel-ps-cli", _ps_cli_login_entry(env), True),
            ("login-cancel-direct", _login_url(env), False),
        )
        for label, entry, require_next in entries:
            problems.extend(
                f"{label}: {problem}"
                for problem in _login_prompt_problems(
                    env, entry, username, label, require_next=require_next
                )
            )
    if problems:
        raise CheckFailedError("; ".join(problems))


def _lockout_page_problems(env: LiveEnv, probe: str) -> list[str]:
    context = env.browser.new_context(ignore_https_errors=env.options.ca is None)
    try:
        page = context.new_page()
        _goto(page, _login_url(env))
        _identify_on_login(page, probe)
        # A message that never shows is reported by lockout_page_problems.
        with contextlib.suppress(Exception):
            page.locator(f"text={REPUTATION_LOCKOUT_MESSAGE}").first.wait_for(
                state="visible", timeout=WALK_TIMEOUT_MS
            )
        _ = _save_page_artifacts(env, page, "lockout")
        return lockout_page_problems(
            message_visible=_visible_text_count(page, REPUTATION_LOCKOUT_MESSAGE) > 0,
            anchors=_cancel_anchors(page),
            go_back_visible=_visible_text_count(page, GO_BACK_TEXT),
        )
    finally:
        context.close()


def _login_reputation_policy(env: LiveEnv) -> dict[str, object]:
    policies = _json_list(env.api(f"/policies/reputation/?name={quote(LOGIN_REPUTATION_POLICY)}"))
    if len(policies) != 1:
        message = f"{len(policies)} reputation policies named {LOGIN_REPUTATION_POLICY}, not one"
        raise CheckFailedError(message)
    return policies[0]


def check_lockout(env: LiveEnv) -> None:
    """Require that a username with a low reputation sees the lockout message and no cancel link.

    Args:
        env: The run's live environment.

    Raises:
        CheckFailedError: The lockout page has a problem, the login reputation policy is not
            found exactly once, or a kubectl or admin API call fails.
        ValueError: The login reputation policy does not check the username.

    Side Effects:
        Creates a disposable probe user and seeds a reputation score for it through `kubectl exec`
        into the Authentik worker; deletes the probe's reputation rows and the user on exit, also
        after a failure.
    """
    probe = new_lockout_probe_username()
    policy = _login_reputation_policy(env)
    user = cast(
        "dict[str, object]", env.api_post("/core/users/", {"username": probe, "name": probe})
    )
    try:
        scores = all_pages(lambda page: env.api(f"/policies/reputation/scores/?page={page}"))
        seed = lockout_seed_score(policy, scores, probe)
        _ = _run(reputation_shell_argv(env.options, probe, score=seed))
        problems = _lockout_page_problems(env, probe)
    finally:
        try:
            _ = _run(reputation_shell_argv(env.options, probe, score=None))
        finally:
            _ = env.api_delete(f"/core/users/{user['pk']}/")
    if problems:
        raise CheckFailedError("; ".join(problems))


def check_unrouted_404(env: LiveEnv) -> None:
    """Require that every unrouted Authentik path answers 404 through the ingress.

    Args:
        env: The run's live environment.

    Raises:
        CheckFailedError: A path of `UNROUTED_PATHS` answers a status other than 404.

    Side Effects:
        Sends one request per path through the ingress.
    """
    wrong = [
        f"{path} -> {status}"
        for path in UNROUTED_PATHS
        if (status := env.ingress_get(path).status) != 404  # noqa: PLR2004  # HTTP 404
    ]
    if wrong:
        raise CheckFailedError("not 404: " + ", ".join(wrong))


CHECKS: Final[dict[str, Callable[[LiveEnv], None]]] = {
    UPGRADE_PRECONDITION: check_upgrade_precondition,
    UPGRADE_STATE_RECOVERY: lambda env: check_upgrade_state(env, RECOVERY_FLOW),
    UPGRADE_STATE_ENROLLMENT: lambda env: check_upgrade_state(env, ENROLLMENT_FLOW),
    FIRST_OWNER: check_first_owner,
    OWNER_RESET: check_owner_reset,
    INVITE: check_invite,
    DONE_PAGE_INPUTS: check_done_page_inputs,
    LOGIN_CANCEL_HIDDEN: check_login_cancel_hidden,
    LOCKOUT: check_lockout,
    UNROUTED_404: check_unrouted_404,
}


def run_check(name: str, env: LiveEnv) -> Outcome:
    """Run one check and report its outcome.

    Args:
        name: A check name from `CHECK_NAMES`.
        env: The run's live environment.

    Returns:
        SKIP with the reason when the options do not allow the check, FAIL with the first line of
        the error when it raises anything, else PASS.

    Side Effects:
        Those of the check that runs.
    """
    reason = skip_reason(name, env.options)
    if reason is not None:
        return Outcome(name, "SKIP", reason)
    try:
        CHECKS[name](env)
    except Exception as error:  # noqa: BLE001  # every failure of a live check is reported, not raised
        return Outcome(name, "FAIL", str(error).splitlines()[0] if str(error) else repr(error))
    return Outcome(name, "PASS")


def _launch_and_run(options: Options) -> list[Outcome]:
    # playwright exists only in this script's uv environment, not in the workspace; a top-level
    # import would stop the fast tests importing the helpers.
    from playwright.sync_api import (  # pyright: ignore[reportMissingImports]  # noqa: PLC0415
        sync_playwright,  # pyright: ignore[reportUnknownVariableType]  # unresolved -> Unknown
    )

    outcomes: list[Outcome] = []
    with (
        sync_playwright() as raw,  # pyright: ignore[reportUnknownVariableType]  # playwright: unresolved -> Unknown
        _authentik_port_forward(options) as local_port,
    ):
        playwright = cast("_Playwright", raw)
        token = _read_api_token(options)
        authentik = playwright.request.new_context(
            base_url=f"http://127.0.0.1:{local_port}{options.authentik_web_path}/",
            extra_http_headers={"Authorization": f"Bearer {token}"},
            ignore_https_errors=False,
        )
        browser = playwright.chromium.launch(headless=True, args=chromium_args(options))
        ingress = playwright.request.new_context(
            base_url=f"https://{options.host}:{options.port}/",
            extra_http_headers={},
            ignore_https_errors=options.ca is None,
        )
        env = LiveEnv(options, playwright, browser, authentik, ingress)
        try:
            for name in options.only:
                outcome = run_check(name, env)
                sys.stdout.write(format_outcome(outcome) + "\n")
                outcomes.append(outcome)
        finally:
            ingress.dispose()
            authentik.dispose()
            browser.close()
    return outcomes


def main(argv: Sequence[str]) -> int:
    """Run the selected checks and print one line each, then the summary.

    Args:
        argv: Command-line arguments without the program name.

    Returns:
        The process exit status: 1 when any check failed or the environment could not start,
        else 0.

    Raises:
        SystemExit: Exit status 2 on a command-line usage error.

    Side Effects:
        Writes to stdout, runs `kubectl` (a port-forward, Secret and ConfigMap reads, and `exec`),
        launches headless Chromium, and performs each selected check's cluster changes.
    """
    options = parse_args(argv)
    try:
        outcomes = _launch_and_run(options)
    except Exception as error:  # noqa: BLE001  # a broken environment is one FAIL line, not a traceback
        hint = (
            f" (install Chromium: {CHROMIUM_INSTALL})"
            if "Executable doesn't exist" in str(error)
            else ""
        )
        outcomes = [Outcome("environment", "FAIL", f"{str(error).splitlines()[0]}{hint}")]
        sys.stdout.write(format_outcome(outcomes[0]) + "\n")
    sys.stdout.write(summary_line(outcomes) + "\n")
    return exit_code(outcomes)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
