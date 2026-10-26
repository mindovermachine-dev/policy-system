"""`scripts/deploy-ps-eval.sh --reset-owner-passkey`: the owner-recovery mode.

The mode reads the installed release, checks the owner against it, finds the owner in Authentik
and prints one fresh single-use enrolment link on the installed hostname, last. It deletes
every Authentik device, session, OAuth token and app-password/API token of the owner, revokes
the owner's signing passkeys in the PS Service pod, and a failed step issues no link and states
what remains.
It never runs helm upgrade, kubectl apply, the TLS or hosts-file steps, and never creates the
owner.
"""

from __future__ import annotations

import base64
import json
import re
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from conftest import EvalFixture, ScriptRun

OWNER_EMAIL = "owner@example.com"
OWNER_PK = 7
TOKEN_VALUE = "s3cr3t-authentik-token-value"
INSTALLED_HOST = "eval.example.test"
PUBLIC_URL = f"https://{INSTALLED_HOST}:30443/auth"
ISSUER = f"{PUBLIC_URL}/application/o/ps-cli/"
RECORD_PATTERN = re.compile(
    r"^owner-passkey-reset \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ owner=\S+ kube_context=\S+"
    r" devices_deleted=\d+ sessions_deleted=\d+ oauth_tokens_revoked=\d+"
    r" core_tokens_deleted=\d+ core_tokens_kept=\d+ signing_credentials_deleted=\d+"
    r" approvals_voided=\d+$"
)
RESET = ("--reset-owner-passkey", "--owner-email", OWNER_EMAIL, "--yes")


def _release_values(**owner: str) -> dict[str, Any]:
    bootstrap = {"subject": OWNER_EMAIL, "issuer": ISSUER, **owner}
    return {
        "psService": {
            "authzBootstrapOwner": {k: v for k, v in bootstrap.items() if v},
            "authentik": {"publicUrl": PUBLIC_URL},
        }
    }


def _owner_user() -> dict[str, Any]:
    return {
        "pk": OWNER_PK,
        "username": OWNER_EMAIL,
        "email": OWNER_EMAIL,
        "type": "internal",
        "groups": [],
        "is_active": True,
    }


def _installed(fixture: EvalFixture) -> None:
    fixture.seed_release(_release_values())
    fixture.seed_curl(users={str(OWNER_PK): _owner_user()})


def _link_lines(run: ScriptRun) -> list[str]:
    return [line for line in run.output.splitlines() if "flow_token=" in line]


def _nothing_changed(fixture: EvalFixture) -> None:
    assert not [c for c in fixture.curl_calls() if "/recovery/" in str(c["url"])]
    assert fixture.helm_upgrades() == []
    assert fixture.curl_users().get(str(OWNER_PK), _owner_user()) == _owner_user()


def test_reset_with_yes_prints_one_link_on_the_installed_hostname_last(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)

    run = eval_fixture.run(*RESET)

    lines = [line for line in run.stdout.splitlines() if line.strip()]
    assert lines[-1].startswith(f"{PUBLIC_URL}/if/flow/")
    assert len(_link_lines(run)) == 1
    assert "register a new passkey" in lines[-2]
    assert "ps.test" not in run.output


def test_completed_reset_prints_one_utc_record_line_before_the_link(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)

    run = eval_fixture.run(*RESET)

    lines = run.stdout.splitlines()
    records = [i for i, line in enumerate(lines) if RECORD_PATTERN.match(line)]
    link = next(i for i, line in enumerate(lines) if "flow_token=" in line)
    assert len(records) == 1
    assert records[0] < link
    assert f"owner={OWNER_EMAIL} kube_context=kind-policy-system " in lines[records[0]]


def test_reset_never_upgrades_helm_or_applies_or_touches_tls_and_hosts(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    hosts = eval_fixture.root / "hosts"
    hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
    security_log = eval_fixture.add_security(trusted=False)

    eval_fixture.run(*RESET, PS_HOSTS_FILE=str(hosts))

    assert all(c.startswith(("status ", "get values ")) for c in eval_fixture.helm_calls())
    verbs = re.compile(r"\b(apply|delete|create|annotate|label|rollout)\b")
    assert not [c for c in eval_fixture.kubectl_calls() if verbs.search(c)]
    assert not eval_fixture.state_dir.exists()
    assert hosts.read_text(encoding="utf-8") == "127.0.0.1 localhost\n"
    assert not security_log.exists()
    assert eval_fixture.curl_users() == {str(OWNER_PK): _owner_user()}


@pytest.mark.parametrize(
    "flag",
    [
        ("--hostname", "other.example.test"),
        ("--lan-address", "192.168.1.5"),
        ("--apply-host-setup",),
        ("--allow-public-host",),
        ("--check-exposure",),
    ],
    ids=lambda f: f[0],
)
def test_reset_rejects_each_deploy_flag_with_a_usage_error(
    eval_fixture: EvalFixture, flag: tuple[str, ...]
) -> None:
    _installed(eval_fixture)

    run = eval_fixture.run(*RESET, *flag, expect=2)

    assert f"--reset-owner-passkey cannot be combined with {flag[0]}" in run.stderr
    assert "usage:" in run.stderr
    assert eval_fixture.kubectl_calls() == []
    assert eval_fixture.helm_calls() == []
    assert eval_fixture.curl_calls() == []


def test_reset_with_yes_and_no_owner_email_is_a_usage_error(eval_fixture: EvalFixture) -> None:
    _installed(eval_fixture)

    run = eval_fixture.run("--reset-owner-passkey", "--yes", expect=2)

    assert "--reset-owner-passkey --yes needs --owner-email <address>" in run.stderr
    assert eval_fixture.kubectl_calls() == []
    assert eval_fixture.curl_calls() == []


def test_reset_without_a_release_says_run_the_full_deploy_and_changes_nothing(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.seed_curl(users={str(OWNER_PK): _owner_user()})

    run = eval_fixture.run(*RESET, expect=1)

    assert "No Helm release policy-system" in run.stderr
    assert "run the full deploy" in run.stderr
    assert eval_fixture.curl_calls() == []
    _nothing_changed(eval_fixture)


@pytest.mark.parametrize(
    ("missing", "named"),
    [
        ("subject", "psService.authzBootstrapOwner.subject"),
        ("issuer", "psService.authzBootstrapOwner.issuer"),
        ("publicUrl", "psService.authentik.publicUrl"),
    ],
)
def test_reset_with_a_release_missing_subject_issuer_or_public_url_fails(
    eval_fixture: EvalFixture, missing: str, named: str
) -> None:
    values = _release_values(**({missing: ""} if missing != "publicUrl" else {}))
    if missing == "publicUrl":
        values["psService"]["authentik"] = {}
    eval_fixture.seed_release(values)
    eval_fixture.seed_curl(users={str(OWNER_PK): _owner_user()})

    run = eval_fixture.run(*RESET, expect=1)

    assert named in run.stderr
    assert "run the full deploy" in run.stderr
    assert eval_fixture.curl_calls() == []
    _nothing_changed(eval_fixture)


def test_reset_owner_email_differing_from_the_release_names_both_values(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)

    run = eval_fixture.run(
        "--reset-owner-passkey", "--owner-email", "someone@example.com", "--yes", expect=1
    )

    assert '"someone@example.com"' in run.stderr
    assert f'"{OWNER_EMAIL}"' in run.stderr
    assert eval_fixture.curl_calls() == []
    _nothing_changed(eval_fixture)


def test_reset_unknown_owner_user_says_run_the_full_deploy(eval_fixture: EvalFixture) -> None:
    eval_fixture.seed_release(_release_values())

    run = eval_fixture.run(*RESET, expect=1)

    assert f'No Authentik user "{OWNER_EMAIL}" exists' in run.stderr
    assert "run the full deploy" in run.stderr
    assert eval_fixture.curl_users() == {}
    assert not [c for c in eval_fixture.curl_calls() if "POST" in c["argv"]]
    assert not _link_lines(run)


@pytest.mark.parametrize(
    ("case", "named"),
    [
        ("token", "Authentik rejected the shared API token"),
        ("cluster", "Cannot reach the cluster of kube context kind-policy-system"),
        ("no_context", "No current kube context"),
        ("not_kind", 'Current kubectl context "docker-desktop" is not a kind-* context'),
    ],
)
def test_reset_rejected_token_or_unreachable_cluster_fails_before_any_change(
    eval_fixture: EvalFixture, case: str, named: str
) -> None:
    _installed(eval_fixture)
    env: dict[str, str] = {}
    if case == "token":
        eval_fixture.seed_curl(token="a-different-token")
    elif case == "cluster":
        env["PS_TEST_CLUSTER_UNREACHABLE"] = "1"
    elif case == "no_context":
        env["PS_TEST_NO_KUBE_CONTEXT"] = "1"
    else:
        env["PS_TEST_KUBE_CONTEXT"] = "docker-desktop"

    run = eval_fixture.run(*RESET, expect=1, **env)

    assert named in run.stderr
    assert not [c for c in eval_fixture.curl_calls() if "POST" in c["argv"]]
    assert not _link_lines(run)
    _nothing_changed(eval_fixture)


def test_reset_prompt_shows_owner_devices_context_and_hostname_and_y_proceeds(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    eval_fixture.seed_curl(devices={str(OWNER_PK): [{"pk": 1}, {"pk": 2}]})

    run = eval_fixture.run("--reset-owner-passkey", "--owner-email", OWNER_EMAIL, stdin="y\n")

    assert f"Owner:                {OWNER_EMAIL}" in run.stderr
    assert "Authentik devices:    2 (deleted)" in run.stderr
    assert "Kube context:         kind-policy-system" in run.stderr
    assert f"Hostname:             {INSTALLED_HOST}" in run.stderr
    assert "Proceed? [y/N]" in run.stderr
    assert "ps.test" not in run.output
    assert len(_link_lines(run)) == 1


@pytest.mark.parametrize("answer", ["n\n", "\n", ""], ids=["n", "empty", "eof"])
def test_reset_declined_or_eof_exits_non_zero_with_no_change(
    eval_fixture: EvalFixture, answer: str
) -> None:
    _installed(eval_fixture)
    eval_fixture.seed_curl(devices={str(OWNER_PK): [{"pk": 1}]})

    run = eval_fixture.run(
        "--reset-owner-passkey", "--owner-email", OWNER_EMAIL, stdin=answer, expect=1
    )

    assert "Proceed? [y/N]" in run.stderr
    assert "Declined; nothing was changed." in run.stderr
    assert not _link_lines(run)
    assert not [c for c in eval_fixture.curl_calls() if "DELETE" in c["argv"]]
    assert not [c for c in eval_fixture.curl_calls() if "POST" in c["argv"]]
    _nothing_changed(eval_fixture)


def test_reset_deletes_every_owner_device_and_records_the_count(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    eval_fixture.seed_curl(devices={str(OWNER_PK): [{"pk": "1"}, {"pk": "2"}]})

    run = eval_fixture.run(*RESET)

    assert eval_fixture.curl_devices() == {str(OWNER_PK): []}
    assert " devices_deleted=2 " in run.stdout
    assert "No Authentik device was found" not in run.output


def test_reset_with_no_devices_says_none_found_and_still_prints_a_link(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)

    run = eval_fixture.run(*RESET)

    assert (
        f"No Authentik device was found for {OWNER_EMAIL}; the sessions, tokens and signing"
        " passkeys were still cleared." in run.output
    )
    assert " devices_deleted=0 " in run.stdout
    assert not [c for c in eval_fixture.curl_calls() if "DELETE" in c["argv"]]
    assert len(_link_lines(run)) == 1


OTHER_PK = 8


def _other_user() -> dict[str, Any]:
    return {**_owner_user(), "pk": OTHER_PK, "username": "other@example.com"}


def _seed_every_credential(fixture: EvalFixture) -> None:
    """The owner and another user, each with devices, sessions, OAuth and core tokens."""
    fixture.seed_curl(
        users={str(OWNER_PK): _owner_user(), str(OTHER_PK): _other_user()},
        devices={str(OWNER_PK): [{"pk": "1"}, {"pk": "2"}], str(OTHER_PK): [{"pk": "3"}]},
        sessions={"s-1": {"user": OWNER_PK}, "s-2": {"user": OWNER_PK}, "s-9": {"user": OTHER_PK}},
        access_tokens={"1": {"user": OWNER_PK}, "9": {"user": OTHER_PK}},
        refresh_tokens={"2": {"user": OWNER_PK}, "8": {"user": OTHER_PK}},
        core_tokens={
            "owner-api": {"user": OWNER_PK, "intent": "api"},
            "owner-app-password": {"user": OWNER_PK, "intent": "app_password"},
            "owner-old-recovery": {"user": OWNER_PK, "intent": "recovery"},
            "other-api": {"user": OTHER_PK, "intent": "api"},
        },
    )


def _seed_signing_passkeys(fixture: EvalFixture, credentials: int = 2) -> None:
    fixture.seed_signing(
        actors={
            f"{OWNER_EMAIL}\n{ISSUER}": {"credentials": credentials, "pending": ["a-1"]},
            f"{OWNER_EMAIL}\nhttps://other.example.test/": {"credentials": 5, "pending": []},
        }
    )


def _signing_execs(fixture: EvalFixture) -> list[str]:
    return [c for c in fixture.kubectl_calls() if "ps_service.passkey_signing.revoke" in c]


def _curl_state(fixture: EvalFixture, key: str) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(fixture.curl_state.read_text()).get(key, {})
    return state


def _index(calls: list[dict[str, Any]], method: str, fragment: str, *, last: bool) -> int:
    hits = [i for i, c in enumerate(calls) if method in c["argv"] and fragment in str(c["url"])]
    assert hits, f"no {method} {fragment}"
    return hits[-1] if last else hits[0]


@pytest.mark.parametrize(
    ("fault", "what", "remaining"),
    [
        (
            {"DELETE /authenticators/admin/webauthn/2/": 500},
            "deleting the owner's devices",
            "devices=1 sessions=2 oauth_tokens=2 core_tokens=3 ",
        ),
        (
            {"DELETE /core/authenticated_sessions/s-2/": 500},
            "deleting the owner's sessions",
            "devices=0 sessions=1 oauth_tokens=2 core_tokens=3 ",
        ),
        (
            {"DELETE /oauth2/access_tokens/": 500},
            "revoking the owner's OAuth tokens",
            "devices=0 sessions=0 oauth_tokens=1 core_tokens=3 ",
        ),
        (
            {"DELETE /core/tokens/owner-old-recovery/": 500},
            "deleting the owner's app-password and API tokens",
            "devices=0 sessions=0 oauth_tokens=0 core_tokens=1 ",
        ),
    ],
    ids=["device_delete", "session_delete", "oauth_delete", "core_token_delete"],
)
def test_a_failed_step_issues_no_link_and_states_what_remains(
    eval_fixture: EvalFixture, fault: dict[str, int], what: str, remaining: str
) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)
    _seed_signing_passkeys(eval_fixture)
    eval_fixture.seed_curl(fail_on=fault)

    run = eval_fixture.run(*RESET, expect=1)

    assert not _link_lines(run)
    assert not [c for c in eval_fixture.curl_calls() if "/recovery/" in str(c["url"])]
    assert f"while {what}" in run.stderr
    assert (
        f"Remaining for {OWNER_EMAIL}: {remaining}signing_credentials=2 pending_approvals=1."
        in run.stderr
    )
    assert "No link was issued. Re-running --reset-owner-passkey is safe" in run.stderr
    assert eval_fixture.signing_state().get("audit", []) == []


def test_a_failed_signing_revoke_issues_no_link_and_states_what_remains(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)
    _seed_signing_passkeys(eval_fixture)
    eval_fixture.seed_signing(fail="revoke", fail_reason="audit_trail_unavailable")

    run = eval_fixture.run(*RESET, expect=1)

    assert not _link_lines(run)
    assert not [c for c in eval_fixture.curl_calls() if "/recovery/" in str(c["url"])]
    assert (
        "The signing passkey step in the PS Service pod failed (audit_trail_unavailable)."
        in run.stderr
    )
    assert (
        f"Remaining for {OWNER_EMAIL}: devices=0 sessions=0 oauth_tokens=0 core_tokens=0"
        " signing_credentials=2 pending_approvals=1." in run.stderr
    )


def test_reset_runs_the_revoke_module_in_the_ps_service_pod_with_the_release_owner(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    _seed_signing_passkeys(eval_fixture, credentials=3)

    run = eval_fixture.run(*RESET)

    expected = (
        "-n default exec deploy/policy-system-ps-service -c ps-service"
        " --pod-running-timeout=60s -- python -m ps_service.passkey_signing.revoke"
        f" --subject {OWNER_EMAIL} --issuer {ISSUER}"
    )
    counted = f"{expected} --count-only"
    assert _signing_execs(eval_fixture) == [counted, expected, counted]
    assert " signing_credentials_deleted=3 approvals_voided=1" in run.stdout
    assert eval_fixture.signing_state()["audit"] == [
        {
            "subject": OWNER_EMAIL,
            "issuer": ISSUER,
            "credentials_deleted": 3,
            "voided_approval_ids": ["a-1"],
        }
    ]
    other = eval_fixture.signing_state()["actors"][f"{OWNER_EMAIL}\nhttps://other.example.test/"]
    assert other == {"credentials": 5, "pending": []}
    assert TOKEN_VALUE not in "\n".join(eval_fixture.kubectl_calls())


def test_reset_prompt_shows_the_signing_credential_count(eval_fixture: EvalFixture) -> None:
    _installed(eval_fixture)
    _seed_signing_passkeys(eval_fixture, credentials=2)

    run = eval_fixture.run("--reset-owner-passkey", "--owner-email", OWNER_EMAIL, stdin="y\n")

    assert "Signing credentials:  2 (deleted)" in run.stderr
    assert "Pending approvals:    1 (voided)" in run.stderr
    assert len(_link_lines(run)) == 1


def test_reset_record_counts_voided_approvals(eval_fixture: EvalFixture) -> None:
    _installed(eval_fixture)
    eval_fixture.seed_signing(
        actors={f"{OWNER_EMAIL}\n{ISSUER}": {"credentials": 0, "pending": ["a-1", "a-2", "a-3"]}}
    )

    run = eval_fixture.run(*RESET)

    lines = run.stdout.splitlines()
    (record,) = [line for line in lines if line.startswith("owner-passkey-reset ")]
    assert record.endswith(" signing_credentials_deleted=0 approvals_voided=3")
    assert eval_fixture.signing_state()["actors"][f"{OWNER_EMAIL}\n{ISSUER}"]["pending"] == []
    assert len(_link_lines(run)) == 1


def test_a_pending_approval_requested_during_the_reset_fails_the_postcondition(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    _seed_signing_passkeys(eval_fixture)
    eval_fixture.seed_signing(pending_after_revoke=["a-new"])

    run = eval_fixture.run(*RESET, expect=1)

    assert not _link_lines(run)
    assert "(pending_approvals=1)" in run.stderr
    assert not [c for c in eval_fixture.curl_calls() if "/recovery/" in str(c["url"])]


@pytest.mark.parametrize(
    "signing",
    [
        pytest.param({"fail": "count"}, id="count_failed"),
        pytest.param({"raw_stdout": "Traceback (most recent call last):\n"}, id="not_json"),
        pytest.param(
            {"raw_stdout": '{"status":"counted","credentials":1,"pending_approvals":0}\nextra\n'},
            id="two_lines",
        ),
    ],
)
def test_an_unreadable_signing_state_fails_before_any_change(
    eval_fixture: EvalFixture, signing: dict[str, Any]
) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)
    eval_fixture.seed_signing(**signing)

    run = eval_fixture.run(*RESET, expect=1)

    assert "The signing passkey step in the PS Service pod failed (" in run.stderr
    assert "nothing was changed" in run.stderr
    assert not [c for c in eval_fixture.curl_calls() if "DELETE" in c["argv"]]
    assert len(_signing_execs(eval_fixture)) == 1
    _nothing_changed(eval_fixture)


def test_a_signing_passkey_enrolled_during_the_reset_fails_the_postcondition(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    _seed_signing_passkeys(eval_fixture)
    eval_fixture.seed_signing(credentials_after_revoke=1)

    run = eval_fixture.run(*RESET, expect=1)

    assert not _link_lines(run)
    assert "signing_credentials=1" in run.stderr
    assert not [c for c in eval_fixture.curl_calls() if "/recovery/" in str(c["url"])]


def test_reset_deletes_sessions_before_the_link_is_issued(eval_fixture: EvalFixture) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)

    run = eval_fixture.run(*RESET)

    assert " sessions_deleted=2 " in run.stdout
    assert _curl_state(eval_fixture, "sessions") == {"s-9": {"user": OTHER_PK}}
    calls = eval_fixture.curl_calls()
    session_delete = _index(calls, "DELETE", "/core/authenticated_sessions/", last=True)
    assert session_delete < _index(calls, "POST", "/recovery/", last=False)


def test_reset_revokes_oauth_tokens_before_the_link_is_issued(eval_fixture: EvalFixture) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)

    run = eval_fixture.run(*RESET)

    assert " oauth_tokens_revoked=2 " in run.stdout
    assert _curl_state(eval_fixture, "access_tokens") == {"9": {"user": OTHER_PK}}
    assert _curl_state(eval_fixture, "refresh_tokens") == {"8": {"user": OTHER_PK}}
    calls = eval_fixture.curl_calls()
    oauth_delete = _index(calls, "DELETE", "/oauth2/", last=True)
    assert oauth_delete < _index(calls, "POST", "/recovery/", last=False)


def test_reset_deletes_old_recovery_links_but_keeps_the_new_one(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)

    run = eval_fixture.run(*RESET)

    assert " core_tokens_deleted=3 core_tokens_kept=0 " in run.stdout
    tokens = _curl_state(eval_fixture, "core_tokens")
    owner_tokens = [t for t in tokens.values() if t["user"] == OWNER_PK]
    assert owner_tokens == [{"user": OWNER_PK, "intent": "recovery"}]
    assert "other-api" in tokens
    assert len(_link_lines(run)) == 1
    assert "Warning: the shared Authentik API token belongs to the owner" not in run.stderr


def test_reset_with_an_owner_owned_token_warns_before_the_link_and_records_the_kept_count(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)
    eval_fixture.seed_curl(token_user_pk=OWNER_PK)

    run = eval_fixture.run(*RESET)

    assert " core_tokens_deleted=2 core_tokens_kept=1 " in run.stdout
    warning = (
        "Warning: the shared Authentik API token belongs to the owner, so 1 of the owner's API"
        " tokens were kept (the one in use cannot be told apart). Fix: move"
        " PS_AUTHENTIK_API_TOKEN in Secret policy-system-authentik-api-token to a token of a"
        " non-owner Authentik user, then re-run --reset-owner-passkey; it then deletes them."
    )
    assert warning in run.stderr
    assert run.stdout.splitlines()[-1].startswith(f"{PUBLIC_URL}/if/flow/")
    assert "owner-api" in _curl_state(eval_fixture, "core_tokens")


def test_reset_prompt_says_the_owners_api_tokens_are_kept_when_the_token_is_the_owners(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    eval_fixture.seed_curl(token_user_pk=OWNER_PK)

    run = eval_fixture.run("--reset-owner-passkey", "--owner-email", OWNER_EMAIL, stdin="y\n")

    assert (
        "  Shared API token:     belongs to this owner; the owner's API tokens are kept"
        in run.stderr
    )


def test_reset_prompt_says_sessions_and_tokens_are_also_revoked(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)

    run = eval_fixture.run("--reset-owner-passkey", "--owner-email", OWNER_EMAIL, stdin="y\n")

    assert (
        "All of the owner's Authentik sessions, OAuth tokens and app-password/API tokens are also\n"
        "revoked, then one new single-use enrolment link is printed." in run.stderr
    )
    assert "Shared API token:" not in run.stderr


def _remaining_line(run: ScriptRun) -> str:
    lines = [line for line in run.stderr.splitlines() if line.startswith("Remaining for ")]
    assert len(lines) == 1, run.stderr
    return lines[0]


def _owner_rows(fixture: EvalFixture, *, except_new_link: bool = False) -> dict[str, int]:
    """Count what the fake Authentik and the fake signing store still hold for the owner.

    With `except_new_link` the recovery token behind a newly issued link is not counted.
    """
    state = json.loads(fixture.curl_state.read_text())
    actor = fixture.signing_state().get("actors", {}).get(f"{OWNER_EMAIL}\n{ISSUER}", {})
    return {
        "devices": len(state["devices"].get(str(OWNER_PK), [])),
        "sessions": sum(1 for s in state["sessions"].values() if s["user"] == OWNER_PK),
        "oauth_tokens": sum(
            1
            for kind in ("access_tokens", "refresh_tokens")
            for t in state[kind].values()
            if t["user"] == OWNER_PK
        ),
        "core_tokens": sum(1 for t in state["core_tokens"].values() if t["user"] == OWNER_PK)
        - sum(1 for k in state["core_tokens"] if except_new_link and k.startswith("ak-recovery-")),
        "signing_credentials": actor.get("credentials", 0),
        "pending_approvals": len(actor.get("pending", [])),
    }


@pytest.mark.parametrize(
    ("fault", "what"),
    [
        ({"GET /core/authenticated_sessions/": 500}, "listing the owner's sessions"),
        ({"GET /oauth2/access_tokens/": 500}, "listing the owner's OAuth tokens"),
        ({"GET /core/tokens/": 500}, "listing the owner's app-password and API tokens"),
        ({"POST /core/users/": 500}, "creating the passkey enrolment link"),
    ],
    ids=["session_list", "oauth_list", "core_token_list", "link_issue"],
)
def test_a_failed_listing_or_link_issue_issues_no_link_and_states_what_remains(
    eval_fixture: EvalFixture, fault: dict[str, int], what: str
) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)
    _seed_signing_passkeys(eval_fixture)
    eval_fixture.seed_curl(fail_on=fault)

    run = eval_fixture.run(*RESET, expect=1)

    assert not _link_lines(run)
    assert f"while {what}" in run.stderr
    remaining = _remaining_line(run)
    rows = _owner_rows(eval_fixture)
    for source, count in rows.items():
        if f"{source}=unknown" not in remaining:
            assert f"{source}={count}" in remaining, (source, remaining)
    assert "No link was issued. Re-running --reset-owner-passkey is safe" in run.stderr


def test_remaining_counts_show_unknown_when_a_source_cannot_be_read(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)
    _seed_signing_passkeys(eval_fixture)
    eval_fixture.seed_curl(fail_on={"GET /core/authenticated_sessions/": 500})

    run = eval_fixture.run(*RESET, expect=1)

    remaining = _remaining_line(run)
    assert remaining.startswith(
        f"Remaining for {OWNER_EMAIL}: devices=0 sessions=unknown oauth_tokens=2 core_tokens=3"
        " signing_credentials=2 pending_approvals=1."
    )
    assert "Could not recount: sessions." in remaining
    assert not _link_lines(run)


@pytest.mark.parametrize(
    "fault",
    [
        {"fail_on": {"DELETE /authenticators/admin/webauthn/2/": 500}},
        {"fail_on": {"DELETE /core/authenticated_sessions/s-2/": 500}},
        {"fail_on": {"DELETE /oauth2/access_tokens/": 500}},
        {"fail_on": {"DELETE /core/tokens/owner-old-recovery/": 500}},
        {"fail_on": {"POST /core/users/": 500}},
        {"signing": "revoke"},
    ],
    ids=["device", "session", "oauth", "core_token", "link_issue", "signing_revoke"],
)
def test_rerunning_after_a_failure_finishes_the_reset(
    eval_fixture: EvalFixture, fault: dict[str, Any]
) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)
    _seed_signing_passkeys(eval_fixture)
    if "signing" in fault:
        eval_fixture.seed_signing(fail=fault["signing"], fail_reason="audit_trail_unavailable")
    else:
        eval_fixture.seed_curl(fail_on=fault["fail_on"])
    failed = eval_fixture.run(*RESET, expect=1)
    remaining = _remaining_line(failed)
    eval_fixture.seed_curl(fail_on={})
    eval_fixture.seed_signing(fail=None)

    rerun = eval_fixture.run(*RESET)

    assert all(f"{s}={n}" in remaining for s, n in _rows_before_rerun(remaining).items())
    assert set(_owner_rows(eval_fixture, except_new_link=True).values()) == {0}
    assert len(_link_lines(rerun)) == 1
    record = next(line for line in rerun.stdout.splitlines() if RECORD_PATTERN.match(line))
    counts = _rows_before_rerun(remaining)
    assert f" devices_deleted={counts['devices']} " in record
    assert f" sessions_deleted={counts['sessions']} " in record
    assert f" oauth_tokens_revoked={counts['oauth_tokens']} " in record
    assert f" core_tokens_deleted={counts['core_tokens']} " in record
    assert f" signing_credentials_deleted={counts['signing_credentials']} " in record
    assert record.endswith(f" approvals_voided={counts['pending_approvals']}")
    other = json.loads(eval_fixture.curl_state.read_text())
    assert other["devices"][str(OTHER_PK)] == [{"pk": "3"}]
    assert sorted(other["sessions"]) == ["s-9"]


def _rows_before_rerun(remaining: str) -> dict[str, int]:
    """The key=N pairs of a remaining-counts line."""
    pairs = re.findall(r"(\w+)=(\d+)", remaining)
    return {key: int(value) for key, value in pairs}


def test_a_device_added_after_the_plan_fails_the_postcondition_without_a_link(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    eval_fixture.seed_curl(
        devices={str(OWNER_PK): [{"pk": "1"}]},
        on_request={
            "DELETE /authenticators/admin/webauthn/": {
                "devices": {str(OWNER_PK): [{"pk": "5", "name": "enrolled-meanwhile"}]}
            }
        },
    )

    run = eval_fixture.run(*RESET, expect=1)

    assert not _link_lines(run)
    assert not [c for c in eval_fixture.curl_calls() if "/recovery/" in str(c["url"])]
    assert "The owner still has credentials or pending approvals after the reset (devices=1)." in (
        run.stderr
    )
    assert _remaining_line(run).startswith(f"Remaining for {OWNER_EMAIL}: devices=1 sessions=0 ")
    assert not RECORD_PATTERN.search(run.stdout)


def test_a_session_started_during_the_reset_fails_the_postcondition_without_a_link(
    eval_fixture: EvalFixture,
) -> None:
    _installed(eval_fixture)
    eval_fixture.seed_curl(
        devices={str(OWNER_PK): [{"pk": "1"}]},
        sessions={},
        on_request={"GET /core/tokens/": {"sessions": {"s-new": {"user": OWNER_PK}}}},
    )

    run = eval_fixture.run(*RESET, expect=1)

    assert not _link_lines(run)
    assert "after the reset (sessions=1)." in run.stderr


def _token_forms() -> tuple[str, str]:
    return TOKEN_VALUE, base64.b64encode(TOKEN_VALUE.encode()).decode()


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_the_token_never_reaches_output_argv_or_logs_and_the_link_appears_exactly_once(
    eval_fixture: EvalFixture, outcome: str
) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)
    _seed_signing_passkeys(eval_fixture)
    if outcome == "failure":
        eval_fixture.seed_curl(fail_on={"DELETE /oauth2/access_tokens/": 500})
    eval_fixture.log_every_tool_argv()

    run = eval_fixture.run(*RESET, expect=0 if outcome == "success" else 1)

    argv = eval_fixture.logged_argv()
    tools = {line.split(" ", 1)[0] for line in argv.splitlines()}
    assert {"kubectl", "helm", "curl", "jq", "base64", "ps-signing-revoke"} <= tools
    logs = [
        run.stdout,
        run.stderr,
        argv,
        eval_fixture.curl_log.read_text(),
        eval_fixture.kubectl_log.read_text(),
        eval_fixture.helm_log.read_text(),
        json.dumps(eval_fixture.signing_state()),
    ]
    for form in _token_forms():
        assert not [text for text in logs if form in text]
    assert run.output.count("flow_token=") == (1 if outcome == "success" else 0)


def test_a_traced_run_never_prints_the_token(eval_fixture: EvalFixture) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)

    run = eval_fixture.run(*RESET, SHELLOPTS="xtrace")

    assert "+ " in run.stderr
    assert "authentik_api" in run.stderr
    for form in _token_forms():
        assert form not in run.output
    assert run.stdout.count("flow_token=") == 1


def test_reset_changes_no_other_user_and_no_role_assignment(eval_fixture: EvalFixture) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)
    before = json.loads(eval_fixture.curl_state.read_text())

    eval_fixture.run(*RESET)

    after = json.loads(eval_fixture.curl_state.read_text())
    assert after["users"] == before["users"]
    assert after["devices"][str(OTHER_PK)] == before["devices"][str(OTHER_PK)]
    for key in ("sessions", "access_tokens", "refresh_tokens", "core_tokens"):
        theirs = {k: v for k, v in before[key].items() if v["user"] == OTHER_PK}
        assert {k: v for k, v in after[key].items() if v["user"] == OTHER_PK} == theirs
    writes = [
        c for c in eval_fixture.curl_calls() if {"POST", "PUT", "PATCH", "DELETE"} & set(c["argv"])
    ]
    assert not [
        c for c in writes if "/core/users/" in str(c["url"]) and "/recovery/" not in str(c["url"])
    ]
    assert not [c for c in eval_fixture.curl_calls() if "/rbac/" in str(c["url"])]
    assert not [c for c in eval_fixture.curl_calls() if "/core/groups/" in str(c["url"])]
    assert all(str(OTHER_PK) not in str(c["url"]).split("?")[0].split("/") for c in writes)


@pytest.mark.parametrize(
    ("case", "named"),
    [
        ("secret", "Cannot read key PS_AUTHENTIK_API_TOKEN from Secret"),
        ("port_forward", "kubectl port-forward to svc/policy-system-authentik-server failed"),
        ("authentik_unreachable", "Cannot reach the Authentik API"),
        ("device_list", "while listing the owner's devices: HTTP 500"),
        ("device_check", "while checking the owner's devices: HTTP 500"),
    ],
)
def test_a_failure_before_any_change_names_it_and_says_nothing_was_changed(
    eval_fixture: EvalFixture, case: str, named: str
) -> None:
    _installed(eval_fixture)
    _seed_every_credential(eval_fixture)
    _seed_signing_passkeys(eval_fixture)
    env: dict[str, str] = {}
    if case == "secret":
        env["PS_TEST_SECRET_B64"] = ""
    elif case == "port_forward":
        env["PS_TEST_PF_FAIL"] = "1"
    elif case == "authentik_unreachable":
        eval_fixture.seed_curl(unreachable=True)
    elif case == "device_list":
        eval_fixture.seed_curl(fail_on={"GET /authenticators/admin/all/": 500})
    else:
        eval_fixture.seed_curl(fail_on={"GET /authenticators/admin/webauthn/": 500})
    before = json.loads(eval_fixture.curl_state.read_text())

    run = eval_fixture.run(*RESET, expect=1, **env)

    assert named in run.stderr
    assert "nothing was changed." in run.stderr
    assert "Remaining for" not in run.stderr
    assert not _link_lines(run)
    assert json.loads(eval_fixture.curl_state.read_text()) == before
    assert not [c for c in eval_fixture.curl_calls() if {"POST", "DELETE"} & set(c["argv"])]
    assert eval_fixture.signing_state().get("audit", []) == []


def test_eval_help_lists_reset_owner_passkey(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--help")

    assert run.stdout.startswith("usage: ")
    assert "--reset-owner-passkey [--owner-email <address>] [--yes]" in run.stdout
    assert "lost or stolen" in run.stdout
    assert "single-use enrolment link" in run.stdout
    assert eval_fixture.curl_calls() == []
