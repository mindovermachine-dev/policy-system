"""Tests for `scripts/deploy-ps-eval.sh` (issue #165, Slice 3D).

The real script and libs run in a temp tree; only the process boundary is faked (kubectl, helm,
podman/docker, openssl network calls and, here, certificate generation, and the stateful
Authentik-API `curl`). Acceptance: AC-BI-007/008/009 (script half), AC-BI-012, AC-BI-013,
AC-BI-016, AC-BI-004/002 (script side), AC-BI-023.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import EvalFixture

# Mirror deploy_eval/conftest.py's own constants (hardcoded, same precedent as deploy_ps).
OWNER_EMAIL = "owner@example.com"
NODE_IP = "10.89.0.7"
TOKEN = "s3cr3t-authentik-token-value"
HOST = "authentik.local"
ISSUER = f"https://{HOST}:30443/application/o/ps-cli/"
AUTH_BASE = f"https://{HOST}:30443"


def _mutations(fixture: EvalFixture) -> list[str]:
    """Anything that changes state: helm, a kubectl apply, or an Authentik API call."""
    kubectl = [c for c in fixture.kubectl_calls() if "apply" in c or "create secret" in c]
    return [*fixture.helm_upgrades(), *kubectl, *fixture.curl_urls()]


# --- happy path: one deploy sets everything (AC-BI-012) ----------------------------------------


def test_fresh_run_deploys_once_with_owner_subject_issuer_and_base_url(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert len(eval_fixture.helm_upgrades()) == 1
    values = eval_fixture.deployed_values()
    ps = values["psService"]
    assert ps["auth"] == {
        "issuer": ISSUER,
        "audience": "ps-cli",
        "cliClientId": "ps-cli",
        "scopes": "openid profile email offline_access",
    }
    assert ps["authzBootstrapOwner"] == {"subject": OWNER_EMAIL, "issuer": ISSUER}
    assert ps["authentik"] == {"baseUrl": AUTH_BASE}
    assert ps["authentikHostname"] == HOST
    assert ps["authentikHostAliasIP"] == NODE_IP
    assert ps["localTestBypass"] == {"enabled": False}
    assert values["authentik"]["enabled"] is True
    assert values["localTls"] == {"secretName": "policy-system-local-tls"}
    assert values["llm"] == {"existingSecret": "policy-system-llm-credentials"}


def test_authentik_pods_get_the_tls_secret_mount_and_a_certificate_checksum(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    global_values = eval_fixture.deployed_values()["authentik"]["global"]
    assert global_values["volumes"] == [
        {"name": "ps-tls", "secret": {"secretName": "policy-system-local-tls"}}
    ]
    assert global_values["volumeMounts"] == [
        {"name": "ps-tls", "mountPath": "/ps-tls", "readOnly": True}
    ]
    assert len(global_values["podAnnotations"]["checksum/local-tls"]) >= 32


def test_helm_installs_the_chart_named_by_ps_chart_ref(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL, PS_CHART_REF="/somewhere/chart")

    assert "upgrade --install policy-system /somewhere/chart" in eval_fixture.helm_upgrades()[0]


def test_default_chart_is_the_published_oci_chart(eval_fixture: EvalFixture) -> None:
    eval_fixture.run(
        "--owner-email",
        OWNER_EMAIL,
        PS_CHART_REF="",
    )

    assert (
        "oci://ghcr.io/mindovermachine-dev/charts/policy-system"
        in (eval_fixture.helm_upgrades()[0])
    )


def test_custom_hostname_drives_issuer_base_url_and_certificate(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL, "--hostname", "ps.example.test")

    values = eval_fixture.deployed_values()["psService"]
    assert values["auth"]["issuer"] == "https://ps.example.test:30443/application/o/ps-cli/"
    assert values["authentik"]["baseUrl"] == "https://ps.example.test:30443"
    leaf = (eval_fixture.state_dir / "leaf.pem").read_text()
    assert "DNS:ps.example.test" in leaf


def test_deployments_are_awaited_after_helm(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    rollouts = [c for c in eval_fixture.kubectl_calls() if "rollout status" in c]
    for name in ("authentik-server", "authentik-worker", "ps-service"):
        assert any(f"deployment/policy-system-{name}" in r for r in rollouts), name


# --- owner: prompt, flags, link (AC-BI-002/004, AC-BI-016) ------------------------------------


def test_email_is_prompted_for_when_no_flag_is_given(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run(stdin=f"{OWNER_EMAIL}\n")

    assert "Owner email" in run.output
    assert any(u["username"] == OWNER_EMAIL for u in eval_fixture.curl_users().values())


def test_empty_prompt_answer_is_rejected_before_any_change(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run(stdin="\n", expect=1)

    assert "--owner-email" in run.stderr
    assert _mutations(eval_fixture) == []


def test_yes_without_an_email_fails_without_prompting(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--yes", expect=1)

    assert "--owner-email" in run.stderr
    assert "Owner email (" not in run.output
    assert _mutations(eval_fixture) == []


def test_yes_with_an_email_runs_non_interactively(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--yes", "--owner-email", OWNER_EMAIL)

    assert len(eval_fixture.helm_upgrades()) == 1


@pytest.mark.parametrize("bad", ["foo", "a@", "with space@x.com"])
def test_invalid_email_is_rejected_before_any_state_change(
    eval_fixture: EvalFixture, bad: str
) -> None:
    run = eval_fixture.run("--owner-email", bad, expect=1)

    assert "not a valid address" in run.stderr
    assert _mutations(eval_fixture) == []


def test_owner_user_is_created_and_the_link_is_printed_once_with_the_https_host(
    eval_fixture: EvalFixture,
) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    users = list(eval_fixture.curl_users().values())
    assert len(users) == 1
    assert users[0]["username"] == users[0]["email"] == OWNER_EMAIL
    links = [ln for ln in run.stdout.splitlines() if ln.startswith(f"{AUTH_BASE}/if/flow/")]
    assert len(links) == 1
    assert "flow_token=" in links[0]


def test_authentik_api_is_called_through_the_external_host_with_the_local_ca(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    calls = eval_fixture.curl_calls()
    assert calls
    for call in calls:
        assert call["url"].startswith(f"{AUTH_BASE}/api/v3/")
        argv = call["argv"]
        assert argv[argv.index("--cacert") + 1] == str(eval_fixture.state_dir / "ca.pem")
        assert argv[argv.index("--resolve") + 1] == f"{HOST}:30443:127.0.0.1"


def test_rerun_with_an_enrolled_owner_issues_no_new_link_and_no_second_deploy(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)
    pk = next(iter(eval_fixture.curl_users()))
    eval_fixture.seed_curl(devices={pk: [{"pk": 1, "name": "passkey"}]})
    before = len(eval_fixture.curl_urls())

    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert len(eval_fixture.helm_upgrades()) == 1
    assert "already has a passkey" in run.stdout
    assert "/if/flow/" not in run.stdout
    new_urls = eval_fixture.curl_urls()[before:]
    assert not any(u.endswith("/recovery/") or "/recovery/?" in u for u in new_urls)
    assert len(eval_fixture.curl_users()) == 1


def test_rerun_with_an_unenrolled_owner_issues_a_fresh_link(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert f"{AUTH_BASE}/if/flow/" in run.stdout
    assert len(eval_fixture.curl_users()) == 1


def test_unreachable_api_fails_naming_the_fix_leaves_no_user_and_a_rerun_completes(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.seed_curl(unreachable=True)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=1)

    assert "Cannot reach the Authentik API" in run.stderr
    assert eval_fixture.curl_users() == {}
    eval_fixture.seed_curl(unreachable=False)
    eval_fixture.run("--owner-email", OWNER_EMAIL)
    assert len(eval_fixture.curl_users()) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_shared_token_prints_the_upgrade_fix_and_creates_nothing(
    eval_fixture: EvalFixture, status: int
) -> None:
    eval_fixture.seed_curl(token="a-different-token", token_reject_status=status)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=1)

    assert "existingSecret" in run.stderr
    assert "does not rotate" in run.stderr
    assert eval_fixture.curl_users() == {}


def test_the_shared_token_never_reaches_output_argv_or_logs(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert TOKEN not in eval_fixture.everything_observable(run)
    assert TOKEN not in "\n".join(eval_fixture.helm_calls())


# --- local TLS (AC-BI-007/008/023) -------------------------------------------------------------


def test_the_tls_secret_carries_leaf_key_and_ca_and_is_applied_before_helm(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    kube = eval_fixture.kubectl_calls()
    create = next(c for c in kube if "create secret generic policy-system-local-tls" in c)
    assert f"--from-file=tls.crt={eval_fixture.state_dir}/leaf.pem" in create
    assert f"--from-file=tls.key={eval_fixture.state_dir}/leaf.key" in create
    assert f"--from-file=ca.crt={eval_fixture.state_dir}/ca.pem" in create
    assert kube.index(create) < next(i for i, c in enumerate(kube) if "rollout status" in c)


def test_ca_is_created_once_and_reused_by_a_rerun(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)
    ca_first = (eval_fixture.state_dir / "ca.pem").read_text()

    eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert (eval_fixture.state_dir / "ca.pem").read_text() == ca_first


def test_no_certificate_refresh_when_authentik_already_serves_the_local_leaf(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.served("local")

    eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert not any(u.endswith("/apply/") for u in eval_fixture.curl_urls())
    assert not [c for c in eval_fixture.kubectl_calls() if "port-forward" in c]


def test_a_different_served_certificate_applies_the_blueprint_over_loopback_http(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.served("other", "other", "local")

    eval_fixture.run("--owner-email", OWNER_EMAIL, PS_TEST_PF_PORT="41999")

    urls = eval_fixture.curl_urls()
    applies = [u for u in urls if u.endswith("/apply/")]
    assert len(applies) == 1
    assert applies[0].startswith("http://127.0.0.1:41999/api/v3/managed/blueprints/")
    assert not any(
        "rollout restart deployment/policy-system-authentik" in c
        for c in eval_fixture.kubectl_calls()
    )
    forwards = [c for c in eval_fixture.kubectl_calls() if "port-forward" in c]
    assert len(forwards) == 1
    assert "svc/policy-system-authentik-server :80" in forwards[0]
    # once the certificate is right, the owner is provisioned through the external https host
    assert any(u.startswith(f"{AUTH_BASE}/api/v3/core/users/") for u in urls)


def test_a_certificate_that_never_arrives_fails_with_a_fix(eval_fixture: EvalFixture) -> None:
    eval_fixture.served("other")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=1)

    assert "not serving the local certificate" in run.stderr
    assert eval_fixture.curl_users() == {}


# --- prerequisites and guard rails (AC-BI-007 missing-tool path, AC-BI-016) --------------------


def test_missing_tools_are_reported_together_with_hints_before_any_change(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.remove_tool("openssl")
    eval_fixture.remove_tool("jq")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=1)

    assert "openssl" in run.stderr
    assert "jq" in run.stderr
    assert "installation-guide" in run.stderr
    assert _mutations(eval_fixture) == []


def test_a_container_engine_is_required(eval_fixture: EvalFixture) -> None:
    eval_fixture.remove_tool("podman")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=1)

    assert "podman" in run.stderr
    assert "docker" in run.stderr
    assert _mutations(eval_fixture) == []


def test_docker_is_used_for_the_node_ip_when_podman_is_absent(eval_fixture: EvalFixture) -> None:
    eval_fixture.remove_tool("podman")
    eval_fixture.add_container_engine("docker")

    eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert eval_fixture.deployed_values()["psService"]["authentikHostAliasIP"] == NODE_IP


def test_the_node_container_name_follows_the_kind_context(eval_fixture: EvalFixture) -> None:
    eval_fixture.env_extra["PS_TEST_PODMAN_LOG"] = str(eval_fixture.root / "podman.log")

    eval_fixture.run("--owner-email", OWNER_EMAIL, PS_TEST_KUBE_CONTEXT="kind-ps-165-lc2")

    calls = [
        json.loads(line) for line in (eval_fixture.root / "podman.log").read_text().splitlines()
    ]
    inspect = next(c for c in calls if c[0] == "inspect")
    assert "ps-165-lc2-control-plane" in inspect


def test_an_unresolvable_node_ip_fails_with_a_fix(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_TEST_PODMAN_INSPECT_EXIT="1", expect=1)

    assert "kind node" in run.stderr
    assert _mutations(eval_fixture) == []


def test_a_non_kind_context_is_refused_before_any_change(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_TEST_KUBE_CONTEXT="aks-prod", expect=1)

    assert "kind-*" in run.stderr
    assert _mutations(eval_fixture) == []


def test_a_missing_llm_secret_points_at_the_guide_step(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_TEST_LLM_SECRET_MISSING="1", expect=1)

    assert "policy-system-llm-credentials" in run.stderr
    assert "sync-llm-secrets-to-kind.sh" in run.stderr
    assert _mutations(eval_fixture) == []


def test_unknown_flag_is_a_usage_error(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--bogus", expect=2)

    assert "usage:" in run.stderr


def test_a_failed_rollout_stops_the_run_with_a_fix_and_creates_no_user(
    eval_fixture: EvalFixture,
) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_TEST_ROLLOUT_EXIT="1", expect=1)

    assert "kubectl get pods" in run.stderr
    assert eval_fixture.curl_users() == {}


# --- closing output (AC-BI-009 script half) ----------------------------------------------------


def _hosts_file(fixture: EvalFixture, content: str = "") -> str:
    path = fixture.root / "hosts"
    path.write_text(content)
    return str(path)


def _fake_sudo(fixture: EvalFixture) -> Path:
    """A `sudo` that logs its argv; `tee` really runs (against the test hosts file)."""
    log = fixture.root / "sudo.log"
    sudo = fixture.bin_dir / "sudo"
    sudo.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "' + str(log) + '"\n'
        '[[ "$1" == tee ]] && exec "$@"\nexit 0\n'
    )
    sudo.chmod(0o755)
    return log


def test_closing_output_lists_the_missing_local_steps_and_points_at_the_guide(
    eval_fixture: EvalFixture,
) -> None:
    hosts = _hosts_file(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=hosts)

    assert f"echo '127.0.0.1 {HOST}' | sudo tee -a {hosts}" in run.stdout
    assert "sudo security add-trusted-cert" in run.stdout
    assert str(eval_fixture.state_dir / "ca.pem") in run.stdout
    assert "installation-guide.md#8-register-your-passkey-and-log-in-with-ps-cli" in run.stdout
    assert "--apply-host-setup" in run.stdout
    # The LAN-colleague walkthrough lives in the guide, not in the script output.
    assert "LAN colleagues" in run.stdout
    assert "export SSL_CERT_FILE" not in run.stdout
    assert run.stdout.rstrip().splitlines()[-1].startswith("https://")


def _fake_ps_cli(fixture: EvalFixture, *, current: str = "") -> Path:
    """A `ps-cli` that logs its argv; `get-contexts` prints `current` (a table row)."""
    log = fixture.root / "ps-cli.log"
    cli = fixture.bin_dir / "ps-cli"
    cli.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "' + str(log) + '"\n'
        "[[ \"$2\" == get-contexts ]] && printf '%s\\n' '" + current + "'\nexit 0\n"
    )
    cli.chmod(0o755)
    return log


def test_ps_cli_context_is_set_and_selected_when_missing(eval_fixture: EvalFixture) -> None:
    log = _fake_ps_cli(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    calls = log.read_text().splitlines()
    assert "config set-context eval --url http://127.0.0.1:8000" in calls
    assert "config use-context eval" in calls
    assert "  ps-cli auth login" in run.stdout
    assert "SSL_CERT_FILE" not in run.stdout
    assert "ps-cli config set-context" not in run.stdout


def test_ps_cli_context_is_left_alone_when_already_current(eval_fixture: EvalFixture) -> None:
    """set-context deletes the stored login, so a re-run must not repeat it."""
    log = _fake_ps_cli(eval_fixture, current="│ *  │ eval │ http://127.0.0.1:8000 │ - │")

    eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert all("set-context" not in call for call in log.read_text().splitlines())


def test_without_ps_cli_the_closing_output_prints_the_commands(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert "ps-cli config set-context eval --url http://127.0.0.1:8000" in run.stdout
    assert "ps-cli config use-context eval" in run.stdout
    assert "  ps-cli auth login" in run.stdout
    assert "SSL_CERT_FILE" not in run.stdout


def test_closing_output_omits_the_hosts_step_when_the_hostname_is_already_mapped(
    eval_fixture: EvalFixture,
) -> None:
    hosts = _hosts_file(eval_fixture, f"127.0.0.1 localhost\n127.0.0.1 {HOST} # ps\n")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=hosts)

    assert "sudo tee -a" not in run.stdout
    assert "add-trusted-cert" in run.stdout


def test_without_consent_no_sudo_command_is_run(eval_fixture: EvalFixture) -> None:
    hosts = _hosts_file(eval_fixture)
    sudo_log = _fake_sudo(eval_fixture)

    eval_fixture.run("--owner-email", OWNER_EMAIL, "--yes", PS_HOSTS_FILE=hosts)

    assert not sudo_log.exists()
    assert (eval_fixture.root / "hosts").read_text() == ""


def test_apply_host_setup_maps_the_hostname_and_trusts_the_ca_through_sudo(
    eval_fixture: EvalFixture,
) -> None:
    hosts = _hosts_file(eval_fixture)
    sudo_log = _fake_sudo(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts)

    assert f"127.0.0.1 {HOST}" in (eval_fixture.root / "hosts").read_text()
    assert f"tee -a {hosts}" in sudo_log.read_text()
    assert "sudo tee -a" not in run.stdout


def test_apply_host_setup_does_nothing_when_the_machine_is_already_set_up(
    eval_fixture: EvalFixture,
) -> None:
    hosts = _hosts_file(eval_fixture, f"127.0.0.1 {HOST}\n")
    sudo_log = _fake_sudo(eval_fixture)
    # CA trust is only checkable on macOS, so make the script see Darwin whatever the host is.
    # `uname` in bin_dir is a symlink to the real binary: unlink before writing, or the write
    # would go through the link and overwrite the system `uname`.
    uname = eval_fixture.bin_dir / "uname"
    uname.unlink()
    uname.write_text("#!/usr/bin/env bash\necho Darwin\n")
    uname.chmod(0o755)
    security = eval_fixture.bin_dir / "security"
    security.write_text("#!/usr/bin/env bash\nexit 0\n")
    security.chmod(0o755)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts)

    assert not sudo_log.exists()
    assert "This machine is ready" in run.stdout


# --- helm idempotence --------------------------------------------------------------------------


def test_second_run_makes_no_helm_upgrade_when_nothing_changed(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert len(eval_fixture.helm_upgrades()) == 1
    assert "unchanged" in run.output.lower()


def test_a_changed_owner_email_triggers_a_new_helm_upgrade(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    eval_fixture.run("--owner-email", "other@example.com")

    assert len(eval_fixture.helm_upgrades()) == 2
    subject = eval_fixture.deployed_values()["psService"]["authzBootstrapOwner"]["subject"]
    assert subject == "other@example.com"


# --- order: Authentik first, then the certificate, then PS Service (found live at LC2) ---------


def _index(calls: list[str], needle: str) -> int:
    return next(i for i, c in enumerate(calls) if needle in c)


def test_ps_service_is_awaited_only_after_authentik_serves_the_certificate(
    eval_fixture: EvalFixture,
) -> None:
    """PS Service discovers the OIDC issuer at startup and crash-loops until Authentik serves the
    local certificate, so the script must not block on PS Service before that certificate is
    served (a fresh install applies it through the port-forward first).
    """
    eval_fixture.served("other", "other", "local")

    eval_fixture.run("--owner-email", OWNER_EMAIL)

    kube = eval_fixture.kubectl_calls()
    authentik = _index(kube, "rollout status deployment/policy-system-authentik-worker")
    forward = _index(kube, "port-forward")
    ps_service = _index(kube, "rollout status deployment/policy-system-ps-service")
    assert authentik < forward < ps_service


def test_ps_service_is_restarted_when_the_certificate_had_to_be_refreshed(
    eval_fixture: EvalFixture,
) -> None:
    """It may be in CrashLoopBackOff from the window before the certificate was served; a restart
    skips the remaining back-off instead of waiting minutes.
    """
    eval_fixture.served("other", "other", "local")

    eval_fixture.run("--owner-email", OWNER_EMAIL)

    kube = eval_fixture.kubectl_calls()
    restart = _index(kube, "rollout restart deployment/policy-system-ps-service")
    assert restart < _index(kube, "rollout status deployment/policy-system-ps-service")


def test_ps_service_is_not_restarted_when_the_certificate_was_already_served(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.served("local")

    eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert not any("rollout restart" in c for c in eval_fixture.kubectl_calls())
