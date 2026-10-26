"""The pure parts of `scripts/verify_enrolment_pages_live.py`.

The script itself runs only against a deployed evaluator release, in its own uv environment
with Playwright. Its browser import sits inside the function that launches the browser, so this
test loads the module from its file and exercises argument parsing, link extraction, the output
lines and exit codes, the done-page checks and the Playwright pin, with no browser and no cluster.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ps_service.api.enrolment_done import ENROLMENT_DONE_PATH

if TYPE_CHECKING:
    import types

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_enrolment_pages_live.py"


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("verify_enrolment_pages_live", SCRIPT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        message = f"could not load spec for {SCRIPT_PATH}"
        raise ImportError(message)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


live = _load_script()


def test_done_path_is_the_path_ps_service_serves_the_done_page_on() -> None:
    assert live.DONE_PATH == ENROLMENT_DONE_PATH


def test_playwright_is_pinned_to_one_exact_version_in_the_script_header() -> None:
    header = SCRIPT_PATH.read_text(encoding="utf-8").split('"""', 1)[0]
    pins = re.findall(r'"playwright([^"]*)"', header)

    assert len(pins) == 1
    assert re.fullmatch(r"==\d+\.\d+\.\d+", pins[0]), pins


def test_the_chromium_install_command_names_the_pinned_version() -> None:
    header = SCRIPT_PATH.read_text(encoding="utf-8").split('"""', 1)[0]
    pin = re.findall(r'"playwright(==[^"]*)"', header)[0]

    assert f"playwright{pin} playwright install chromium" in live.CHROMIUM_INSTALL


def test_default_run_selects_every_check_except_the_upgrade_precondition() -> None:
    options = live.parse_args([])

    assert options.only == tuple(n for n in live.CHECK_NAMES if n != "upgrade-precondition")
    assert options.host == "ps.test"
    assert options.port == 30443
    assert options.allow_owner_reset is False


def test_only_selects_the_named_checks_in_the_given_order() -> None:
    options = live.parse_args(["--only", "unrouted-404,upgrade-state:recovery"])

    assert options.only == ("unrouted-404", "upgrade-state:recovery")


def test_an_unknown_check_name_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        live.parse_args(["--only", "no-such-check"])

    assert raised.value.code == 2


def test_the_last_https_link_of_deploy_output_is_the_enrolment_link() -> None:
    output = (
        "Using https://ps.test:30443 as the public URL\n"
        "Open this link to register a passkey:\n"
        "  https://ps.test:30443/auth/if/flow/ps-passkey-recovery/?flow_token=abc.\n"
    )

    assert (
        live.last_https_link(output)
        == "https://ps.test:30443/auth/if/flow/ps-passkey-recovery/?flow_token=abc"
    )


def test_deploy_output_without_a_link_yields_none() -> None:
    assert live.last_https_link("no link here\n") is None


def test_outcome_lines_and_summary_name_every_skip() -> None:
    outcomes = [
        live.Outcome("unrouted-404", "PASS"),
        live.Outcome("first-owner", "SKIP", "no --first-owner-link given"),
        live.Outcome("owner-reset", "FAIL", "final path is '/auth/'"),
    ]

    assert [live.format_outcome(o) for o in outcomes] == [
        "PASS unrouted-404",
        "SKIP first-owner (no --first-owner-link given)",
        "FAIL owner-reset: final path is '/auth/'",
    ]
    assert live.summary_line(outcomes) == "SUMMARY 1 passed, 1 failed, skipped: first-owner"


def test_any_fail_exits_1_and_passes_with_skips_exit_0() -> None:
    passed = live.Outcome("unrouted-404", "PASS")
    skipped = live.Outcome("first-owner", "SKIP", "no --first-owner-link given")
    failed = live.Outcome("owner-reset", "FAIL", "x")

    assert live.exit_code([passed, skipped]) == 0
    assert live.exit_code([passed, failed]) == 1
    assert live.summary_line([passed]) == "SUMMARY 1 passed, 0 failed, skipped: none"


def test_first_owner_and_owner_reset_skip_without_their_arguments() -> None:
    options = live.parse_args(["--owner-email", "owner@example.com"])

    assert live.skip_reason("first-owner", options) == "no --first-owner-link given"
    assert "--allow-owner-reset" in live.skip_reason("owner-reset", options)
    assert live.skip_reason("unrouted-404", options) is None


def test_owner_reset_runs_only_with_the_opt_in_and_an_owner_email() -> None:
    allowed = live.parse_args(["--allow-owner-reset", "--owner-email", "owner@example.com"])
    no_email = live.parse_args(["--allow-owner-reset"])

    assert live.skip_reason("owner-reset", allowed) is None
    assert live.skip_reason("owner-reset", no_email) == "no --owner-email given"


def test_the_done_page_with_its_text_and_no_links_has_no_problems() -> None:
    text = "Passkey registered. Your passkey is registered. Run ps-cli auth login."

    assert live.done_page_problems(ENROLMENT_DONE_PATH, text, 0) == []


def test_a_denied_page_with_a_link_on_another_path_reports_each_problem() -> None:
    problems = live.done_page_problems(
        "/auth/if/flow/ps-passkey-recovery/", "Request has been denied.", 2
    )

    assert len(problems) == 4
    assert any("denied" in p for p in problems)
    assert any("2 link(s)" in p for p in problems)


def test_every_check_name_has_a_check() -> None:
    assert set(live.CHECKS) == set(live.CHECK_NAMES)


def test_the_invite_and_enrollment_upgrade_checks_are_named_and_run_by_default() -> None:
    options = live.parse_args([])

    assert "invite" in live.CHECK_NAMES
    assert "upgrade-state:enrollment" in live.CHECK_NAMES
    assert "invite" in options.only
    assert "upgrade-state:enrollment" in options.only
    assert live.skip_reason("invite", options) is None


def test_the_invite_link_opens_the_enrollment_flow_with_the_invitation_token() -> None:
    link = live.invite_link("https://ps.test:30443", "/auth", "0d6f-pk")

    assert link == "https://ps.test:30443/auth/if/flow/ps-invite-enrollment/?itoken=0d6f-pk"


def test_a_generated_invitee_name_is_disposable_and_an_owner_name_is_not() -> None:
    username = live.new_invitee_username()

    assert live.is_disposable_invitee(username)
    assert live.new_invitee_username() != username
    assert not live.is_disposable_invitee("owner@example.com")
    assert not live.is_disposable_invitee("live-invitee-")
    assert not live.is_disposable_invitee(username + "x")


def test_only_web_links_are_probed_once_each_without_their_fragment() -> None:
    hrefs = [
        "https://ps.test:30443/enrolled#top",
        "https://ps.test:30443/enrolled",
        "mailto:someone@example.com",
        "javascript:void(0)",
        "http://ps.test/auth/",
    ]

    assert live.probeable_links(hrefs) == ["https://ps.test:30443/enrolled", "http://ps.test/auth/"]


_DONE_HEADERS = {
    "content-security-policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


def test_done_page_inputs_is_a_named_check_with_five_request_variants() -> None:
    assert "done-page-inputs" in live.CHECK_NAMES
    assert len(live.DONE_PAGE_VARIANTS) == 5
    assert live.DONE_PAGE_VARIANTS[0].query == ""
    assert live.DONE_PAGE_VARIANTS[0].headers == {}


def test_identical_done_page_answers_with_the_headers_have_no_problems() -> None:
    answers = [
        live.DoneAnswer(variant.label, 200, b"<p>registered</p>", dict(_DONE_HEADERS))
        for variant in live.DONE_PAGE_VARIANTS
    ]

    assert live.done_page_input_problems(answers) == []


def test_a_reflected_input_a_changed_body_and_a_missing_header_are_each_reported() -> None:
    plain = live.DoneAnswer("plain", 200, b"<p>registered</p>", dict(_DONE_HEADERS))
    reflected = live.DoneAnswer("flow-token", 200, b"<p>registered abc123</p>", dict(_DONE_HEADERS))
    no_csp = live.DoneAnswer(
        "bearer",
        503,
        b"<p>registered</p>",
        {k: v for k, v in _DONE_HEADERS.items() if k != "content-security-policy"},
    )

    problems = live.done_page_input_problems([plain, reflected, no_csp])

    assert any("flow-token" in p and "differs" in p for p in problems)
    assert any("flow-token" in p and "abc123" in p for p in problems)
    assert any("bearer" in p and "503" in p for p in problems)
    assert any("bearer" in p and "content-security-policy" in p for p in problems)


def test_the_login_cancel_and_lockout_checks_are_named_and_run_by_default() -> None:
    options = live.parse_args([])

    assert "login-cancel-hidden" in options.only
    assert "lockout" in options.only
    assert live.skip_reason("login-cancel-hidden", options) is None
    assert live.skip_reason("lockout", options) is None


_CANCEL = "https://ps.test:30443/auth/flows/-/cancel/"


def test_a_hidden_cancel_link_carrying_next_on_a_next_entry_has_no_problems() -> None:
    anchors = [live.CancelAnchor(f"{_CANCEL}?next=%2Fauth%2Fdevice", visible=False)]
    url = "https://ps.test:30443/auth/if/flow/ps-login/?next=%2Fauth%2Fdevice%3Fcode%3DX"

    assert live.login_cancel_problems(url, anchors, 0, require_next=True) == []


def test_a_visible_cancel_link_and_a_visible_not_you_are_each_reported() -> None:
    anchors = [live.CancelAnchor(_CANCEL, visible=True)]

    problems = live.login_cancel_problems(
        "https://ps.test:30443/auth/if/flow/ps-login/", anchors, 1, require_next=False
    )

    assert any("1 visible" in p for p in problems)
    assert any("Not you?" in p for p in problems)


def test_no_attached_cancel_link_is_reported_so_a_pass_proves_the_css_hides_it() -> None:
    problems = live.login_cancel_problems(
        "https://ps.test:30443/auth/if/flow/ps-login/", [], 0, require_next=False
    )

    assert problems == ["no cancel link is attached to the passkey prompt"]


def test_a_ps_cli_entry_without_next_in_the_url_or_the_cancel_link_is_reported() -> None:
    anchors = [live.CancelAnchor(_CANCEL, visible=False)]

    problems = live.login_cancel_problems(
        "https://ps.test:30443/auth/if/flow/ps-login/", anchors, 0, require_next=True
    )

    assert len(problems) == 2
    assert any("page URL" in p for p in problems)
    assert any("?next=" in p and "cancel link" in p for p in problems)


def test_the_lockout_page_with_its_message_and_no_cancel_link_has_no_problems() -> None:
    anchors = [live.CancelAnchor(_CANCEL, visible=False)]

    assert (
        live.lockout_page_problems(message_visible=True, anchors=anchors, go_back_visible=0) == []
    )


def test_a_lockout_page_without_its_message_and_with_a_visible_go_back_reports_both() -> None:
    anchors = [live.CancelAnchor(_CANCEL, visible=True)]

    problems = live.lockout_page_problems(message_visible=False, anchors=anchors, go_back_visible=1)

    assert any(live.REPUTATION_LOCKOUT_MESSAGE in p for p in problems)
    assert any("1 visible" in p for p in problems)
    assert any("Go back" in p for p in problems)


def test_a_generated_lockout_probe_name_is_disposable_and_an_owner_name_is_not() -> None:
    probe = live.new_lockout_probe_username()

    assert re.fullmatch(r"lockout-probe-[0-9a-f]{12}", probe)
    assert live.is_lockout_probe(probe)
    assert not live.is_lockout_probe("owner@example.com")
    assert not live.is_lockout_probe(probe + "'")


def test_the_reputation_seed_runs_ak_shell_on_the_worker_with_the_probe_as_one_argv_element() -> (
    None
):
    options = live.parse_args(["--namespace", "ps", "--release", "rel"])
    probe = live.new_lockout_probe_username()

    argv = live.reputation_shell_argv(options, probe, score=-22)

    assert argv[:8] == [
        "kubectl",
        "-n",
        "ps",
        "exec",
        "deploy/rel-authentik-worker",
        "--",
        "ak",
        "shell",
    ]
    assert argv[8] == "-c"
    assert len(argv) == 10
    assert f"identifier={probe!r}" in argv[9]
    assert "score=-22" in argv[9]
    assert "objects.create(" in argv[9]


def test_the_reputation_cleanup_deletes_only_the_probe_rows() -> None:
    options = live.parse_args([])
    probe = live.new_lockout_probe_username()

    code = live.reputation_shell_argv(options, probe, score=None)[-1]

    assert f"filter(identifier={probe!r}).delete()" in code
    assert "create(" not in code


def test_the_reputation_command_refuses_a_name_that_is_not_a_generated_probe() -> None:
    options = live.parse_args([])

    with pytest.raises(ValueError, match="lockout probe"):
        live.reputation_shell_argv(options, "x'); import os; os.system('id'); ('", score=-10)


_LOGIN_POLICY = {"check_ip": True, "check_username": True, "threshold": -5}


def _score(identifier: str, ip: str, score: int) -> dict[str, object]:
    return {"identifier": identifier, "ip": ip, "score": score}


def test_with_no_other_reputation_the_seed_is_the_margin_below_the_threshold() -> None:
    probe = live.new_lockout_probe_username()

    seed = live.lockout_seed_score(_LOGIN_POLICY, [], probe)

    assert seed == -5 - live.LOCKOUT_SCORE_MARGIN


def test_the_seed_cancels_the_highest_positive_score_any_shared_client_ip_holds() -> None:
    probe = live.new_lockout_probe_username()
    rows = [
        _score("owner@example.com", "10.244.0.1", 5),
        _score("live-invitee-0123456789ab", "10.244.0.1", 1),
        _score("live-invitee-ba9876543210", "10.244.0.1", 1),
        _score("someone", "10.244.0.7", 3),
    ]

    seed = live.lockout_seed_score(_LOGIN_POLICY, rows, probe)

    assert 7 + seed <= -5 - live.LOCKOUT_SCORE_MARGIN
    assert 3 + seed <= -5 - live.LOCKOUT_SCORE_MARGIN
    assert seed == -5 - live.LOCKOUT_SCORE_MARGIN - 7


def test_a_negative_ip_total_never_raises_the_seed_above_the_margin() -> None:
    probe = live.new_lockout_probe_username()

    seed = live.lockout_seed_score(_LOGIN_POLICY, [_score("x", "10.244.0.1", -4)], probe)

    assert seed == -5 - live.LOCKOUT_SCORE_MARGIN


def test_a_row_matching_both_the_ip_and_the_probe_is_counted_once() -> None:
    probe = live.new_lockout_probe_username()
    rows = [_score(probe, "10.244.0.1", 2), _score("other", "10.244.0.1", 1)]

    seed = live.lockout_seed_score(_LOGIN_POLICY, rows, probe)

    assert seed == -5 - live.LOCKOUT_SCORE_MARGIN - 3


def test_without_an_ip_check_only_the_probe_rows_count() -> None:
    probe = live.new_lockout_probe_username()
    policy = {**_LOGIN_POLICY, "check_ip": False, "threshold": -8}
    rows = [_score("owner@example.com", "10.244.0.1", 5), _score(probe, "0.0.0.0", 1)]

    seed = live.lockout_seed_score(policy, rows, probe)

    assert seed == -8 - live.LOCKOUT_SCORE_MARGIN - 1


def test_a_policy_that_ignores_the_username_cannot_be_seeded_per_probe() -> None:
    probe = live.new_lockout_probe_username()

    with pytest.raises(ValueError, match="username"):
        live.lockout_seed_score({**_LOGIN_POLICY, "check_username": False}, [], probe)


_DEPLOYED = "version: 1\nmetadata:\n  name: PS Service\n"


def _instance(status: str, applied: str) -> dict[str, object]:
    return {
        "name": f"{live.BLUEPRINT_NAME_PREFIX} (bundled)",
        "status": status,
        "last_applied_hash": live.blueprint_content_hash(applied),
    }


def test_the_blueprint_hash_is_the_sha512_of_its_utf8_text_as_authentik_records_it() -> None:
    assert live.blueprint_content_hash("a — b") == hashlib.sha512("a — b".encode()).hexdigest()


def test_a_successful_apply_of_the_deployed_content_is_applied_without_a_newer_apply() -> None:
    instances = [{"name": "Default - Authentication flow"}, _instance("successful", _DEPLOYED)]

    assert live.blueprint_pending_reason(instances, _DEPLOYED) is None


def test_an_applied_blueprint_older_than_the_deployed_content_is_pending() -> None:
    reason = live.blueprint_pending_reason([_instance("successful", "old")], _DEPLOYED)

    assert reason is not None
    assert "not the deployed" in reason


@pytest.mark.parametrize("status", ["error", "orphaned", "unknown", "warning"])
def test_an_apply_that_is_not_successful_is_pending_even_with_the_deployed_hash(
    status: str,
) -> None:
    reason = live.blueprint_pending_reason([_instance(status, _DEPLOYED)], _DEPLOYED)

    assert reason is not None
    assert status in reason


def test_a_missing_bundled_blueprint_is_pending() -> None:
    reason = live.blueprint_pending_reason([{"name": "Default - Authentication flow"}], _DEPLOYED)

    assert reason is not None
    assert "not found" in reason


def test_without_resolve_to_requests_and_chromium_use_the_host_as_given() -> None:
    options = live.parse_args([])

    assert options.resolve_to is None
    assert live.pinned_request("https://ps.test:30443/enrolled?x=1", options) == (
        "https://ps.test:30443/enrolled?x=1",
        {},
    )
    assert live.chromium_args(options) == []


def test_resolve_to_sends_ingress_requests_to_the_address_with_the_host_header() -> None:
    options = live.parse_args(["--resolve-to", "127.0.0.1"])

    assert live.pinned_request("https://ps.test:30443/auth/?a=b", options) == (
        "https://127.0.0.1:30443/auth/?a=b",
        {"Host": "ps.test:30443"},
    )


def test_resolve_to_leaves_a_link_to_another_host_unchanged() -> None:
    options = live.parse_args(["--resolve-to", "127.0.0.1"])

    assert live.pinned_request("https://example.org/x", options) == ("https://example.org/x", {})


def test_resolve_to_maps_the_host_to_the_address_in_chromium() -> None:
    options = live.parse_args(["--host", "ps.test", "--resolve-to", "127.0.0.1"])

    assert live.chromium_args(options) == ["--host-resolver-rules=MAP ps.test 127.0.0.1"]


def test_every_page_of_a_paginated_api_list_is_read() -> None:
    pages = {
        1: {"pagination": {"next": 2}, "results": [{"name": "a"}]},
        2: {"pagination": {"next": 0}, "results": [{"name": "b"}]},
    }
    requested: list[int] = []

    def fetch(page: int) -> object:
        requested.append(page)
        return pages[page]

    assert live.all_pages(fetch) == [{"name": "a"}, {"name": "b"}]
    assert requested == [1, 2]


def test_a_list_without_pagination_is_read_once() -> None:
    def fetch(_page: int) -> object:
        return {"results": [{"name": "a"}]}

    assert live.all_pages(fetch) == [{"name": "a"}]


def test_webauthn_devices_are_counted_by_the_type_authentik_reports() -> None:
    rows: list[object] = [
        {"type": "authentik_stages_authenticator_webauthn.WebAuthnDevice"},
        {"type": "authentik_stages_authenticator_totp.TOTPDevice"},
        {"type": "authentik_stages_authenticator_webauthn.WebAuthnDevice"},
    ]

    assert live.count_webauthn_devices(rows) == 2


def test_an_answer_that_is_not_a_device_list_counts_no_device() -> None:
    assert live.count_webauthn_devices({"detail": "nope"}) == 0


@pytest.mark.parametrize(
    "message",
    [
        "Page.goto: net::ERR_TIMED_OUT at https://ps.test:30443/auth/if/flow/ps-login/",
        "Page.goto: net::ERR_CONNECTION_RESET at https://ps.test:30443/enrolled",
        "Page.goto: Timeout 15000ms exceeded.",
    ],
)
def test_a_navigation_that_never_reached_the_server_is_a_transport_failure(message: str) -> None:
    assert live.is_navigation_transport_failure(message)


@pytest.mark.parametrize(
    "message",
    [
        "Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://ps.test:30443/",
        "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://ps.test:30443/",
        "Locator.fill: element is not visible",
    ],
)
def test_a_certificate_name_or_page_error_is_not_retried(message: str) -> None:
    assert not live.is_navigation_transport_failure(message)
