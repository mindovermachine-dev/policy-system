"""`scripts/deploy-ps-prod.sh --reset-owner-passkey`: the owner-recovery mode.

The mode works on the current kube context only: no Azure call, no config file, no Helm upgrade.
It reads the installed release, checks the owner against it, finds the owner in Authentik and
prints one fresh single-use enrolment link on the public hostname, last.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from conftest import DeployPsFixture, ScriptRun

PROD_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "deploy-ps-prod.sh"
OWNER_EMAIL = "owner@example.test"
OWNER_PK = 7
PUBLIC_URL = "https://prod.example.test/auth"
ISSUER = f"{PUBLIC_URL}/application/o/ps-cli/"
RECORD_PATTERN = re.compile(
    r"^owner-passkey-reset \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ owner=\S+ kube_context=\S+"
    r" devices_deleted=\d+ sessions_deleted=\d+ oauth_tokens_revoked=\d+"
    r" core_tokens_deleted=\d+ core_tokens_kept=\d+ signing_credentials_deleted=\d+"
    r" approvals_voided=\d+$"
)
RESET = ("--reset-owner-passkey", "--owner-email", OWNER_EMAIL, "--yes")


def _installed(fixture: DeployPsFixture) -> None:
    fixture.seed_helm_release(
        {
            "psService": {
                "authzBootstrapOwner": {"subject": OWNER_EMAIL, "issuer": ISSUER},
                "authentik": {"publicUrl": PUBLIC_URL},
            }
        }
    )
    fixture.seed_curl(
        users={
            str(OWNER_PK): {
                "pk": OWNER_PK,
                "username": OWNER_EMAIL,
                "email": OWNER_EMAIL,
                "type": "internal",
                "groups": [],
                "is_active": True,
            }
        }
    )


def _link_lines(run: ScriptRun) -> list[str]:
    return [line for line in run.output.splitlines() if "flow_token=" in line]


def test_prod_reset_with_yes_prints_one_link_on_the_public_hostname_last(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(*RESET)

    lines = [line for line in run.stdout.splitlines() if line.strip()]
    assert lines[-1].startswith(f"{PUBLIC_URL}/if/flow/")
    assert "register a new passkey" in lines[-2]
    assert len(_link_lines(run)) == 1


def test_completed_prod_reset_prints_one_utc_record_line_before_the_link(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(*RESET)

    lines = run.stdout.splitlines()
    records = [i for i, line in enumerate(lines) if RECORD_PATTERN.match(line)]
    link = next(i for i, line in enumerate(lines) if "flow_token=" in line)
    assert len(records) == 1
    assert records[0] < link
    assert f"owner={OWNER_EMAIL} kube_context=prod-aks " in lines[records[0]]


def test_prod_reset_makes_no_az_call_and_no_helm_upgrade(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(*RESET)

    assert deploy_ps_fixture.read_az_log() == []
    assert deploy_ps_fixture.read_helm_log()
    assert all(
        c.startswith(("status ", "get values ", "list ")) for c in deploy_ps_fixture.read_helm_log()
    )
    assert "EXPERIMENTAL" not in run.stderr
    verbs = re.compile(r"\b(apply|delete|create|annotate|label|rollout)\b")
    assert not [c for c in deploy_ps_fixture.read_kubectl_log() if verbs.search(c)]


def test_prod_reset_with_yes_never_prompts(deploy_ps_fixture: DeployPsFixture) -> None:
    _installed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(*RESET)

    assert "Proceed" not in run.output
    assert "Type the owner email" not in run.output
    assert "Owner email (" not in run.output


@pytest.mark.parametrize(
    "args",
    [
        ("--reset-owner-passkey", "--rotate-key"),
        ("--rotate-key", "--reset-owner-passkey"),
    ],
    ids=["reset-first", "rotate-first"],
)
def test_prod_reset_with_rotate_key_is_a_usage_error(
    deploy_ps_fixture: DeployPsFixture, args: tuple[str, str]
) -> None:
    run = deploy_ps_fixture.run_deploy(*args, expect=2)

    assert "--reset-owner-passkey cannot be combined with --rotate-key" in run.stderr
    assert deploy_ps_fixture.read_az_log() == []
    assert deploy_ps_fixture.read_kubectl_log() == []


def test_prod_reset_with_yes_and_no_owner_email_is_a_usage_error(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    run = deploy_ps_fixture.run_deploy("--reset-owner-passkey", "--yes", expect=2)

    assert "--reset-owner-passkey --yes needs --owner-email <address>" in run.stderr
    assert deploy_ps_fixture.read_kubectl_log() == []


def test_prod_help_lists_reset_owner_passkey_and_exits_zero(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    run = deploy_ps_fixture.run_deploy("--help")

    assert run.stdout.startswith("usage: ")
    assert "--reset-owner-passkey [--owner-email <address>] [--yes]" in run.stdout
    assert deploy_ps_fixture.read_az_log() == []


def test_prod_help_describes_reset_owner_passkey(deploy_ps_fixture: DeployPsFixture) -> None:
    run = deploy_ps_fixture.run_deploy("--help")

    assert "lost or stolen" in run.stdout
    assert "single-use enrolment link" in run.stdout


def test_prod_header_documents_reset_and_no_longer_claims_a_rerun_recovers_a_lost_device() -> None:
    header = PROD_SCRIPT.read_text().split("set -euo pipefail", 1)[0]
    prose = " ".join(line.lstrip("#").strip() for line in header.splitlines())

    assert "--reset-owner-passkey" in prose
    assert "Re-running is the owner-recovery path" not in prose
    assert (
        "Re-running issues a fresh link to an owner with no passkey; a lost or stolen device is"
        " recovered with --reset-owner-passkey." in prose
    )


def test_prod_reset_without_a_release_says_run_the_full_deploy(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    run = deploy_ps_fixture.run_deploy(*RESET, expect=1)

    assert "No Helm release policy-system" in run.stderr
    assert "run the full deploy" in run.stderr
    assert deploy_ps_fixture.read_curl_calls() == []


def test_prod_reset_owner_email_differing_from_the_release_names_both_values(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(
        "--reset-owner-passkey", "--owner-email", "someone@example.test", "--yes", expect=1
    )

    assert '"someone@example.test"' in run.stderr
    assert f'"{OWNER_EMAIL}"' in run.stderr
    assert deploy_ps_fixture.read_curl_calls() == []


@pytest.mark.parametrize(
    ("answer", "proceeds"),
    [(f"{OWNER_EMAIL}\n", True), ("y\n", False), ("someone@example.test\n", False)],
    ids=["exact", "y", "other-email"],
)
def test_prod_reset_requires_typing_the_owner_email(
    deploy_ps_fixture: DeployPsFixture, answer: str, *, proceeds: bool
) -> None:
    _installed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(
        "--reset-owner-passkey",
        "--owner-email",
        OWNER_EMAIL,
        stdin=answer,
        expect=0 if proceeds else 1,
    )

    assert f"Type the owner email ({OWNER_EMAIL}) to proceed:" in run.stderr
    assert "Hostname:             prod.example.test" in run.stderr
    assert "Kube context:         prod-aks" in run.stderr
    if proceeds:
        assert len(_link_lines(run)) == 1
    else:
        assert "Declined; nothing was changed." in run.stderr
        assert not _link_lines(run)
        calls = deploy_ps_fixture.read_curl_calls()
        assert not [c for c in calls if "'POST'" in str(c["argv"]) or "'DELETE'" in str(c["argv"])]


def test_prod_reset_deletes_every_owner_device_and_records_the_count(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)
    deploy_ps_fixture.seed_curl(devices={str(OWNER_PK): [{"pk": "1"}, {"pk": "2"}]})

    run = deploy_ps_fixture.run_deploy(*RESET)

    state = json.loads(deploy_ps_fixture.curl_state.read_text(encoding="utf-8"))
    assert state["devices"] == {str(OWNER_PK): []}
    assert " devices_deleted=2 " in run.stdout
    assert len(_link_lines(run)) == 1


def test_prod_reset_deletes_sessions_oauth_and_core_tokens_and_records_the_counts(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)
    deploy_ps_fixture.seed_curl(
        sessions={"s-1": {"user": OWNER_PK}},
        access_tokens={"1": {"user": OWNER_PK}},
        refresh_tokens={"2": {"user": OWNER_PK}, "3": {"user": OWNER_PK}},
        core_tokens={"owner-app-password": {"user": OWNER_PK, "intent": "app_password"}},
    )

    run = deploy_ps_fixture.run_deploy(*RESET)

    state = json.loads(deploy_ps_fixture.curl_state.read_text(encoding="utf-8"))
    assert state["sessions"] == {}
    assert state["access_tokens"] == {}
    assert state["refresh_tokens"] == {}
    assert [t["intent"] for t in state["core_tokens"].values()] == ["recovery"]
    assert (
        " sessions_deleted=1 oauth_tokens_revoked=3 core_tokens_deleted=1 core_tokens_kept=0 "
        in run.stdout
    )
    assert len(_link_lines(run)) == 1


def test_prod_reset_runs_the_revoke_module_in_the_pod(deploy_ps_fixture: DeployPsFixture) -> None:
    _installed(deploy_ps_fixture)
    deploy_ps_fixture.seed_signing(
        actors={f"{OWNER_EMAIL}\n{ISSUER}": {"credentials": 2, "pending": ["a-1"]}}
    )

    run = deploy_ps_fixture.run_deploy(*RESET)

    expected = (
        "exec deploy/policy-system-ps-service -c ps-service --pod-running-timeout=60s --"
        f" python -m ps_service.passkey_signing.revoke --subject {OWNER_EMAIL} --issuer {ISSUER}"
    )
    execs = [c for c in deploy_ps_fixture.read_kubectl_log() if "passkey_signing.revoke" in c]
    counted = f"{expected} --count-only"
    assert execs == [counted, expected, counted]
    assert " signing_credentials_deleted=2 approvals_voided=1" in run.stdout
    assert deploy_ps_fixture.signing_state()["audit"] == [
        {
            "subject": OWNER_EMAIL,
            "issuer": ISSUER,
            "credentials_deleted": 2,
            "voided_approval_ids": ["a-1"],
        }
    ]
    assert len(_link_lines(run)) == 1


OTHER_PK = 8


def _token_forms(fixture: DeployPsFixture) -> tuple[str, str]:
    """The shared API token (the one the fake Authentik accepts) and its base64 Secret form."""
    token: str = json.loads(fixture.curl_state.read_text(encoding="utf-8"))["token"]
    return token, base64.b64encode(token.encode()).decode()


def _seed_every_credential(fixture: DeployPsFixture) -> None:
    """Seed the owner and another user with every kind of Authentik credential.

    The owner also gets two signing passkeys and a pending approval.
    """
    users = fixture.read_authentik_users()
    other = {**users[str(OWNER_PK)], "pk": OTHER_PK, "username": "other@example.test"}
    fixture.seed_curl(
        users={**users, str(OTHER_PK): other},
        devices={str(OWNER_PK): [{"pk": "1"}, {"pk": "2"}], str(OTHER_PK): [{"pk": "3"}]},
        sessions={"s-1": {"user": OWNER_PK}, "s-9": {"user": OTHER_PK}},
        access_tokens={"1": {"user": OWNER_PK}, "9": {"user": OTHER_PK}},
        refresh_tokens={"2": {"user": OWNER_PK}, "8": {"user": OTHER_PK}},
        core_tokens={
            "owner-app-password": {"user": OWNER_PK, "intent": "app_password"},
            "other-api": {"user": OTHER_PK, "intent": "api"},
        },
    )
    fixture.seed_signing(
        actors={f"{OWNER_EMAIL}\n{ISSUER}": {"credentials": 2, "pending": ["a-1"]}}
    )


def _is_write(call: dict[str, object]) -> bool:
    return any(f"'{method}'" in str(call["argv"]) for method in ("POST", "PUT", "PATCH", "DELETE"))


def _remaining_line(run: ScriptRun) -> str:
    lines = [line for line in run.stderr.splitlines() if line.startswith("Remaining for ")]
    assert len(lines) == 1, run.stderr
    return lines[0]


@pytest.mark.parametrize(
    ("fault", "what", "remaining"),
    [
        (
            {"fail_on": {"DELETE /authenticators/admin/webauthn/2/": 500}},
            "while deleting the owner's devices",
            (
                "devices=1 sessions=1 oauth_tokens=2 core_tokens=1 signing_credentials=2"
                " pending_approvals=1."
            ),
        ),
        (
            {"fail_on": {"DELETE /oauth2/refresh_tokens/": 500}},
            "while revoking the owner's OAuth tokens",
            (
                "devices=0 sessions=0 oauth_tokens=2 core_tokens=1 signing_credentials=2"
                " pending_approvals=1."
            ),
        ),
        (
            {"signing": "revoke"},
            "The signing passkey step in the PS Service pod failed (audit_trail_unavailable).",
            (
                "devices=0 sessions=0 oauth_tokens=0 core_tokens=0 signing_credentials=2"
                " pending_approvals=1."
            ),
        ),
        (
            {"fail_on": {"POST /core/users/": 500}},
            "while creating the passkey enrolment link",
            (
                "devices=0 sessions=0 oauth_tokens=0 core_tokens=0 signing_credentials=0"
                " pending_approvals=0."
            ),
        ),
    ],
    ids=["device_delete", "oauth_delete", "signing_revoke", "link_issue"],
)
def test_prod_failed_step_issues_no_link_and_states_what_remains(
    deploy_ps_fixture: DeployPsFixture, fault: dict[str, object], what: str, remaining: str
) -> None:
    _installed(deploy_ps_fixture)
    _seed_every_credential(deploy_ps_fixture)
    if "signing" in fault:
        deploy_ps_fixture.seed_signing(fail=fault["signing"], fail_reason="audit_trail_unavailable")
    else:
        deploy_ps_fixture.seed_curl(fail_on=fault["fail_on"])

    run = deploy_ps_fixture.run_deploy(*RESET, expect=1)

    assert not _link_lines(run)
    assert what in run.stderr
    assert _remaining_line(run).startswith(f"Remaining for {OWNER_EMAIL}: {remaining}")
    assert "No link was issued. Re-running --reset-owner-passkey is safe" in run.stderr
    assert not RECORD_PATTERN.search(run.stdout)


def test_prod_rerunning_after_a_failure_finishes_the_reset(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)
    _seed_every_credential(deploy_ps_fixture)
    deploy_ps_fixture.seed_curl(fail_on={"DELETE /core/authenticated_sessions/": 500})
    deploy_ps_fixture.run_deploy(*RESET, expect=1)
    deploy_ps_fixture.seed_curl(fail_on={})

    rerun = deploy_ps_fixture.run_deploy(*RESET)

    state = json.loads(deploy_ps_fixture.curl_state.read_text(encoding="utf-8"))
    assert state["devices"] == {str(OWNER_PK): [], str(OTHER_PK): [{"pk": "3"}]}
    assert sorted(state["sessions"]) == ["s-9"]
    assert sorted(state["access_tokens"]) == ["9"]
    assert sorted(state["refresh_tokens"]) == ["8"]
    assert [t["intent"] for t in state["core_tokens"].values() if t["user"] == OWNER_PK] == [
        "recovery"
    ]
    assert (
        " devices_deleted=0 sessions_deleted=1 oauth_tokens_revoked=2 core_tokens_deleted=1"
        " core_tokens_kept=0 signing_credentials_deleted=2 approvals_voided=1" in rerun.stdout
    )
    assert len(_link_lines(rerun)) == 1


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_prod_token_never_in_output_or_argv(
    deploy_ps_fixture: DeployPsFixture, outcome: str
) -> None:
    _installed(deploy_ps_fixture)
    _seed_every_credential(deploy_ps_fixture)
    if outcome == "failure":
        deploy_ps_fixture.seed_curl(fail_on={"DELETE /core/tokens/": 500})
    env = deploy_ps_fixture.log_every_tool_argv()

    run = deploy_ps_fixture.run_deploy(
        *RESET, expect=0 if outcome == "success" else 1, extra_env=env
    )

    argv = deploy_ps_fixture.logged_argv()
    tools = {line.split(" ", 1)[0] for line in argv.splitlines()}
    assert {"kubectl", "helm", "curl", "jq", "base64", "ps-signing-revoke"} <= tools
    logs = [
        run.stdout,
        run.stderr,
        argv,
        deploy_ps_fixture.curl_log.read_text(encoding="utf-8"),
        "\n".join(deploy_ps_fixture.read_kubectl_log()),
        "\n".join(deploy_ps_fixture.read_helm_log()),
        json.dumps(deploy_ps_fixture.signing_state()),
    ]
    for form in _token_forms(deploy_ps_fixture):
        assert not [text for text in logs if form in text]
    assert run.output.count("flow_token=") == (1 if outcome == "success" else 0)


@pytest.mark.parametrize(
    ("case", "named"),
    [
        ("token", "Authentik rejected the shared API token"),
        ("cluster", "Cannot reach the cluster of kube context prod-aks"),
        ("no_context", "No current kube context"),
    ],
)
def test_prod_reset_rejected_token_or_unreachable_cluster_fails_before_any_change(
    deploy_ps_fixture: DeployPsFixture, case: str, named: str
) -> None:
    _installed(deploy_ps_fixture)
    _seed_every_credential(deploy_ps_fixture)
    before = deploy_ps_fixture.curl_state.read_text(encoding="utf-8")
    env: dict[str, str] = {}
    if case == "token":
        deploy_ps_fixture.seed_curl(token="a-different-token")
        before = deploy_ps_fixture.curl_state.read_text(encoding="utf-8")
    elif case == "cluster":
        env["PS_TEST_CLUSTER_UNREACHABLE"] = "1"
    else:
        env["PS_TEST_NO_KUBE_CONTEXT"] = "1"

    run = deploy_ps_fixture.run_deploy(*RESET, expect=1, extra_env=env)

    assert named in run.stderr
    assert not _link_lines(run)
    assert deploy_ps_fixture.curl_state.read_text(encoding="utf-8") == before
    assert not [c for c in deploy_ps_fixture.read_curl_calls() if _is_write(c)]
    assert not [c for c in deploy_ps_fixture.read_kubectl_log() if "exec " in c]
    assert deploy_ps_fixture.signing_state().get("audit", []) == []


def test_prod_reset_changes_no_other_user_and_no_role_assignment(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)
    _seed_every_credential(deploy_ps_fixture)
    before = json.loads(deploy_ps_fixture.curl_state.read_text(encoding="utf-8"))

    deploy_ps_fixture.run_deploy(*RESET)

    after = json.loads(deploy_ps_fixture.curl_state.read_text(encoding="utf-8"))
    assert after["users"] == before["users"]
    assert after["devices"][str(OTHER_PK)] == before["devices"][str(OTHER_PK)]
    for key in ("sessions", "access_tokens", "refresh_tokens", "core_tokens"):
        theirs = {k: v for k, v in before[key].items() if v["user"] == OTHER_PK}
        assert {k: v for k, v in after[key].items() if v["user"] == OTHER_PK} == theirs
    calls = deploy_ps_fixture.read_curl_calls()
    writes = [c for c in calls if _is_write(c)]
    assert [
        str(c["url"]).split("/api/v3", 1)[1] for c in writes if "/core/users/" in str(c["url"])
    ] == [f"/core/users/{OWNER_PK}/recovery/?token_duration=minutes=30"]
    assert not [c for c in calls if "/rbac/" in str(c["url"]) or "/core/groups/" in str(c["url"])]
    assert deploy_ps_fixture.read_az_log() == []
    assert not [c for c in deploy_ps_fixture.read_kubectl_log() if "role" in c.lower()]


def _count_change_surface(fixture: DeployPsFixture) -> str:
    """Everything a reset could change: the fake Authentik state and the signing state."""
    return fixture.curl_state.read_text(encoding="utf-8") + json.dumps(fixture.signing_state())


def test_prod_reset_from_a_bundle_of_another_version_stops_before_any_change(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)
    _seed_every_credential(deploy_ps_fixture)
    deploy_ps_fixture.seed_helm_release(
        deploy_ps_fixture.read_helm_release_values() or {}, chart_version="12.0.1"
    )
    deploy_ps_fixture.write_bundle_version("12.1.0")
    before = _count_change_surface(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(*RESET, expect=1)

    assert "at chart version 12.0.1" in run.stderr
    assert "this bundle is version 12.1.0" in run.stderr
    assert "run the deploy script without --reset-owner-passkey" in run.stderr
    assert not _link_lines(run)
    assert _count_change_surface(deploy_ps_fixture) == before
    assert deploy_ps_fixture.read_curl_calls() == []
    assert not [c for c in deploy_ps_fixture.read_kubectl_log() if "exec " in c]


def test_prod_reset_version_mismatch_is_reported_before_the_owner_email_is_asked(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)
    deploy_ps_fixture.seed_helm_release(
        deploy_ps_fixture.read_helm_release_values() or {}, chart_version="12.0.1"
    )
    deploy_ps_fixture.write_bundle_version("12.1.0")

    run = deploy_ps_fixture.run_deploy("--reset-owner-passkey", expect=1)

    assert "this bundle is version 12.1.0" in run.stderr
    assert "owner email" not in run.output.lower()


def test_prod_reset_from_a_bundle_at_the_deployed_version_reaches_the_signing_step(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)
    deploy_ps_fixture.seed_helm_release(
        deploy_ps_fixture.read_helm_release_values() or {}, chart_version="12.1.0"
    )
    deploy_ps_fixture.write_bundle_version("12.1.0")

    run = deploy_ps_fixture.run_deploy(*RESET)

    assert [c for c in deploy_ps_fixture.read_kubectl_log() if "passkey_signing.revoke" in c]
    assert len(_link_lines(run)) == 1


def test_prod_reset_without_a_version_file_never_asks_helm_for_the_chart_version(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(*RESET)

    assert len(_link_lines(run)) == 1
    assert not [c for c in deploy_ps_fixture.read_helm_log() if c.startswith("list ")]


def test_prod_reset_with_an_unreadable_release_version_fails_closed(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)
    deploy_ps_fixture.write_bundle_version("12.1.0")

    run = deploy_ps_fixture.run_deploy(*RESET, expect=1)

    assert "Could not read the chart version of Helm release policy-system" in run.stderr
    assert deploy_ps_fixture.read_curl_calls() == []


def test_prod_reset_with_a_local_chart_override_skips_the_version_check(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)
    deploy_ps_fixture.seed_helm_release(
        deploy_ps_fixture.read_helm_release_values() or {}, chart_version="0.0.0-dev"
    )
    deploy_ps_fixture.write_bundle_version("12.1.0")

    run = deploy_ps_fixture.run_deploy(*RESET, extra_env={"PS_CHART_REF": "./charts/policy-system"})

    assert len(_link_lines(run)) == 1
    assert not [c for c in deploy_ps_fixture.read_helm_log() if c.startswith("list ")]


def test_prod_reset_against_an_image_without_the_revoke_module_shows_the_pod_error(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _installed(deploy_ps_fixture)
    _seed_every_credential(deploy_ps_fixture)
    deploy_ps_fixture.seed_signing(
        stderr="python: No module named ps_service.passkey_signing.revoke\n"
    )
    before = deploy_ps_fixture.curl_state.read_text(encoding="utf-8")

    run = deploy_ps_fixture.run_deploy(*RESET, expect=1)

    assert "failed (python: No module named ps_service.passkey_signing.revoke)." in run.stderr
    assert "no result" not in run.stderr
    assert "nothing was changed" in run.stderr
    assert not _link_lines(run)
    assert deploy_ps_fixture.curl_state.read_text(encoding="utf-8") == before
    assert not [c for c in deploy_ps_fixture.read_curl_calls() if _is_write(c)]
