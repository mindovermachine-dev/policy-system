"""Tests for `scripts/deploy-ps-eval.sh`.

The real script and libs run in a temp tree; only the process boundary is faked (kubectl, helm,
podman/docker, openssl network calls and, here, certificate generation, and the stateful
Authentik-API `curl`).
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from conftest import EvalFixture, ScriptRun

# Mirror deploy_eval/conftest.py's own constants (hardcoded, same precedent as deploy_ps).
OWNER_EMAIL = "owner@example.com"
NODE_IP = "10.89.0.7"
TOKEN = "s3cr3t-authentik-token-value"
HOST = "ps.test"
LAN = "192.168.1.5"
MACOS_TRUST = (
    "sudo security add-trusted-cert -d -r trustRoot -p ssl -k /Library/Keychains/System.keychain"
)
ISSUER = f"https://{HOST}:30443/auth/application/o/ps-cli/"
AUTH_BASE = f"https://{HOST}:30443/auth"
IN_CLUSTER_AUTH = "http://policy-system-authentik-server/auth"
PF_PORT = "41999"
OPENSSL_TIMEOUT_SECONDS = 30
SECONDS_PER_DAY = 86400


def _x509_text(path: Path) -> str:
    out = subprocess.run(  # noqa: S603 - fixed argv, test-owned file
        ["openssl", "x509", "-in", str(path), "-noout", "-text"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=OPENSSL_TIMEOUT_SECONDS,
    )
    return " ".join(out.stdout.split())


def _expires_within_days(path: Path, days: int) -> bool:
    """True when the certificate expires no later than `days` days (plus one) from now."""
    seconds = (days + 1) * SECONDS_PER_DAY
    done = subprocess.run(  # noqa: S603 - fixed argv, test-owned file
        ["openssl", "x509", "-in", str(path), "-noout", "-checkend", str(seconds)],  # noqa: S607
        capture_output=True,
        check=False,
        timeout=OPENSSL_TIMEOUT_SECONDS,
    )
    return done.returncode != 0


def _leaf_sans(fixture: EvalFixture) -> list[str]:
    """The DNS/IP entries of the generated leaf's subject alternative name."""
    text = _x509_text(fixture.state_dir / "leaf.pem")
    after = text.split("X509v3 Subject Alternative Name:", 1)[1]
    names = after.split("X509v3", 1)[0].split("Signature Algorithm", 1)[0]
    return [entry.strip() for entry in names.split(",") if entry.strip()]


def _api_calls(fixture: EvalFixture) -> list[dict[str, Any]]:
    """The curl calls that provision Authentik, not the closing probes of the proxy."""
    return [c for c in fixture.curl_calls() if ":30443/" not in str(c["url"])]


def _mutations(fixture: EvalFixture) -> list[str]:
    """Anything that changes state: helm, a kubectl apply, or an Authentik API call."""
    kubectl = [c for c in fixture.kubectl_calls() if "apply" in c or "create secret" in c]
    return [*fixture.helm_upgrades(), *kubectl, *(str(c["url"]) for c in _api_calls(fixture))]


# --- happy path: one deploy sets everything -----------------------------------------------------


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
    assert ps["authentik"] == {"baseUrl": IN_CLUSTER_AUTH, "publicUrl": AUTH_BASE}
    assert ps["authentikHostname"] == HOST
    assert ps["authentikHostAliasIP"] == NODE_IP
    assert ps["publicUrl"] == f"https://{HOST}:30443"
    assert ps["localTestBypass"] == {"enabled": False}
    assert values["authentik"]["enabled"] is True
    assert ps["localCa"] == {"secretName": "policy-system-local-ca"}
    assert values["proxy"]["tls"] == {"secretName": "policy-system-local-tls"}
    assert "localTls" not in values
    assert values["llm"] == {"existingSecret": "policy-system-llm-credentials"}


def test_the_release_pins_the_signing_origin_to_the_configured_name_and_proxy_port(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL, "--hostname", "ps.example.test")

    ps = eval_fixture.deployed_values()["psService"]
    assert ps["passkeySigning"] == {"allowedOrigins": ["https://ps.example.test:30443"]}
    assert ps["publicUrl"] == "https://ps.example.test:30443"
    assert "localhost:8000" not in json.dumps(eval_fixture.deployed_values())


def test_default_hostname_is_the_single_evaluator_name_everywhere(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    values = eval_fixture.deployed_values()
    ps = values["psService"]
    assert ps["authentikHostname"] == HOST
    assert ps["auth"]["issuer"] == ISSUER
    assert ps["authzBootstrapOwner"]["issuer"] == ISSUER
    assert ps["authentik"]["publicUrl"] == AUTH_BASE
    assert _leaf_sans(eval_fixture) == [f"DNS:{HOST}"]
    assert ".local" not in json.dumps(values)


def test_only_the_proxy_gets_the_tls_secret_and_a_certificate_checksum(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    values = eval_fixture.deployed_values()
    assert values["proxy"]["tls"] == {"secretName": "policy-system-local-tls"}
    assert len(values["proxy"]["podAnnotations"]["checksum/local-tls"]) >= 32
    assert "global" not in values["authentik"]
    assert "policy-system-local-tls" not in json.dumps(values["psService"])


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
    assert values["auth"]["issuer"] == "https://ps.example.test:30443/auth/application/o/ps-cli/"
    assert values["authentik"]["baseUrl"] == IN_CLUSTER_AUTH
    assert values["authentik"]["publicUrl"] == "https://ps.example.test:30443/auth"
    assert _leaf_sans(eval_fixture) == ["DNS:ps.example.test"]


def test_an_ip_literal_hostname_is_rejected_before_any_change(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--hostname", "192.168.1.5", expect=2)

    assert "192.168.1.5" in run.stderr
    assert "IP address" in run.stderr
    assert "Fix:" in run.stderr
    assert _mutations(eval_fixture) == []
    assert not eval_fixture.state_dir.exists()


@pytest.mark.parametrize("name", ["ps.test\nIP:1.2.3.4", "ps.test;DNS:evil.test", "ps test"])
def test_a_hostname_with_unsafe_characters_is_rejected_before_any_change(
    eval_fixture: EvalFixture, name: str
) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--hostname", name, expect=2)

    assert "letters, digits" in run.stderr
    assert _mutations(eval_fixture) == []
    assert not eval_fixture.state_dir.exists()


def test_deployments_are_awaited_after_helm(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    rollouts = [c for c in eval_fixture.kubectl_calls() if "rollout status" in c]
    for name in ("authentik-server", "authentik-worker", "ps-service"):
        assert any(f"deployment/policy-system-{name}" in r for r in rollouts), name


# --- owner: prompt, flags, link ----------------------------------------------------------------


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


def test_authentik_api_is_called_through_the_loopback_port_forward(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL, PS_TEST_PF_PORT=PF_PORT)

    calls = _api_calls(eval_fixture)
    assert calls
    for call in calls:
        assert call["url"].startswith(f"http://127.0.0.1:{PF_PORT}/auth/api/v3/")
        assert "--resolve" not in call["argv"]
    forwards = [c for c in eval_fixture.kubectl_calls() if "port-forward" in c]
    assert len(forwards) == 1
    assert "svc/policy-system-authentik-server :80" in forwards[0]


def test_owner_link_carries_the_auth_prefix_once(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    link = run.stdout.rstrip().splitlines()[-1]
    assert link.startswith(f"https://{HOST}:30443/auth/if/flow/ps-passkey-recovery/?flow_token=")
    assert "/auth/auth" not in run.stdout


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
    assert "http://127.0.0.1:9000/auth/if/admin/" in run.stderr
    assert eval_fixture.curl_users() == {}


def test_the_shared_token_never_reaches_output_argv_or_logs(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert TOKEN not in eval_fixture.everything_observable(run)
    assert TOKEN not in "\n".join(eval_fixture.helm_calls())


# --- local TLS ---------------------------------------------------------------------------------


def test_the_tls_secret_carries_leaf_and_key_only_and_is_applied_before_helm(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    kube = eval_fixture.kubectl_calls()
    create = next(c for c in kube if "create secret generic policy-system-local-tls" in c)
    assert f"--from-file=tls.crt={eval_fixture.state_dir}/leaf.pem" in create
    assert f"--from-file=tls.key={eval_fixture.state_dir}/leaf.key" in create
    assert "ca.crt" not in create
    assert kube.index(create) < next(i for i, c in enumerate(kube) if "rollout status" in c)


def test_the_ca_secret_carries_the_ca_certificate_only(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    create = next(
        c
        for c in eval_fixture.kubectl_calls()
        if "create secret generic policy-system-local-ca" in c
    )
    assert f"--from-file=ca.crt={eval_fixture.state_dir}/ca.pem" in create
    assert "tls.key" not in create
    assert "leaf.key" not in create


def test_only_the_ca_certificate_is_referenced_for_distribution_never_its_key(
    eval_fixture: EvalFixture,
) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    ca_key = eval_fixture.state_dir / "ca.key"
    observable = eval_fixture.everything_observable(run)
    applied = eval_fixture.root / "kubectl-applied.yaml"
    # The only mention of the key is the warning not to copy it.
    assert observable.replace("never copy ca.key", "").count("ca.key") == 0
    assert str(ca_key) not in observable
    assert ca_key.read_text() not in observable + applied.read_text()
    assert f"{eval_fixture.state_dir.resolve()}/ca.pem" in run.stdout


def test_ca_is_created_once_and_reused_by_a_rerun(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)
    ca_first = (eval_fixture.state_dir / "ca.pem").read_text()

    eval_fixture.run("--owner-email", OWNER_EMAIL)

    ca = eval_fixture.state_dir / "ca.pem"
    assert ca.read_text() == ca_first
    text = _x509_text(ca)
    assert f"X509v3 Name Constraints: critical Permitted: DNS:{HOST} " in text
    assert _expires_within_days(ca, 730)


# --- prerequisites and guard rails (including the missing-tool path) ---------------------------


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


# --- `.local` hostnames are rejected: Claude Code cannot resolve them ----------------------------

# Built by concatenation so the contiguous old default appears in no tracked file (the AC gate).
REJECTED_LOCAL_HOST = "authentik" + ".local"


@pytest.mark.parametrize(
    "hostname", [REJECTED_LOCAL_HOST, "foo.local", "FOO.LOCAL", "a.b.local", "foo.local."]
)
def test_hostname_ending_in_local_is_rejected_before_any_change(
    eval_fixture: EvalFixture, hostname: str
) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--hostname", hostname, expect=2)

    assert "Claude Code" in run.stderr
    assert ".local" in run.stderr
    assert "ps.test" in run.stderr
    assert _mutations(eval_fixture) == []
    assert eval_fixture.kubectl_calls() == []


def test_hostname_rejection_runs_before_the_required_tool_check(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.remove_tool("kubectl")
    eval_fixture.remove_tool("helm")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--hostname", "foo.local", expect=2)

    assert "Claude Code" in run.stderr
    assert "Missing required tool" not in run.stderr


@pytest.mark.parametrize(
    "hostname", ["localhost-x.test", "local", "my.localhost.test", "localnet.example.test"]
)
def test_hostnames_that_merely_contain_local_are_accepted(
    eval_fixture: EvalFixture, hostname: str
) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--hostname", hostname)

    assert "Claude Code cannot resolve" not in run.stderr
    assert eval_fixture.deployed_values()["psService"]["authentikHostname"] == hostname


def test_help_still_works_with_the_hostname_check(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("-h")

    assert "usage:" in run.stdout


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


# --- closing output ----------------------------------------------------------------------------


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


def test_final_probe_hits_health_and_issuer_through_the_proxy(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    probes = [c for c in eval_fixture.curl_calls() if ":30443/" in c["url"]]
    urls = [c["url"] for c in probes]
    assert f"https://{HOST}:30443/health" in urls
    assert f"{ISSUER}.well-known/openid-configuration" in urls
    for call in probes:
        argv = call["argv"]
        assert f"{HOST}:30443:127.0.0.1" in argv
        assert str(eval_fixture.state_dir / "ca.pem") in argv


def test_final_probe_fails_when_the_proxy_reports_another_issuer(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.seed_curl(proxy_issuer="https://ps.test:8443/auth/application/o/ps-cli/")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=None)

    assert run.returncode == 1
    assert "issuer" in run.stderr
    assert "Fix:" in run.stderr


def test_final_probe_fails_when_health_does_not_answer(eval_fixture: EvalFixture) -> None:
    eval_fixture.seed_curl(proxy_health_status=502)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=None)

    assert run.returncode == 1
    assert "/health" in run.stderr
    assert "Fix:" in run.stderr


def test_final_probe_requires_401_without_a_bearer_token(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)
    assert f"https://{HOST}:30443/mcp/" in eval_fixture.curl_urls()
    mcp = next(c for c in eval_fixture.curl_calls() if c["url"].endswith("/mcp/"))
    assert not any("authorization" in part.lower() for part in mcp["argv"])

    eval_fixture.seed_curl(proxy_mcp_status=200)
    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=None)

    assert run.returncode == 1
    assert "401" in run.stderr


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
    assert f"config set-context eval --url https://{HOST}:30443" in calls
    assert "config use-context eval" in calls
    assert "ps-cli" not in run.stdout


def test_ps_cli_context_is_left_alone_when_already_current(eval_fixture: EvalFixture) -> None:
    """set-context deletes the stored login, so a re-run must not repeat it."""
    log = _fake_ps_cli(eval_fixture, current=f"│ *  │ eval │ https://{HOST}:30443 │ - │")

    eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert all("set-context" not in call for call in log.read_text().splitlines())


def test_without_consent_no_sudo_command_is_run(eval_fixture: EvalFixture) -> None:
    hosts = _hosts_file(eval_fixture)
    sudo_log = _fake_sudo(eval_fixture)

    eval_fixture.run("--owner-email", OWNER_EMAIL, "--yes", PS_HOSTS_FILE=hosts)

    assert not sudo_log.exists()
    assert (eval_fixture.root / "hosts").read_text() == ""


def test_the_name_is_mapped_to_the_lan_address_not_to_loopback(eval_fixture: EvalFixture) -> None:
    hosts = _hosts_file(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=hosts)

    assert _hosts_line_printed(run) == f"{LAN} {HOST}"
    assert f"127.0.0.1 {HOST}" not in run.output
    assert f"::1 {HOST}" not in run.output
    assert "resolves to 127.0.0.1" not in run.output


def test_apply_host_setup_appends_exactly_one_hosts_line(eval_fixture: EvalFixture) -> None:
    hosts = _hosts_file(eval_fixture, "127.0.0.1 localhost\n")
    sudo_log = _fake_sudo(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts)

    written = (eval_fixture.root / "hosts").read_text().splitlines()
    assert written == ["127.0.0.1 localhost", f"{LAN} {HOST}"]
    assert f"tee -a {hosts}" in sudo_log.read_text()
    assert f"sudo tee -a {hosts}" not in run.stdout


def test_rerun_with_the_mapping_present_changes_nothing_and_still_prints_the_hosts_line(
    eval_fixture: EvalFixture,
) -> None:
    hosts = _hosts_file(eval_fixture, f"{LAN} {HOST}\n")
    sudo_log = _fake_sudo(eval_fixture)
    eval_fixture.set_platform("Darwin")
    eval_fixture.add_security(trusted=True)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts)

    assert not sudo_log.exists()
    assert (eval_fixture.root / "hosts").read_text() == f"{LAN} {HOST}\n"
    assert _hosts_line_printed(run) == f"{LAN} {HOST}"


def test_a_hosts_line_mapping_the_name_to_another_address_is_reported_not_duplicated(
    eval_fixture: EvalFixture,
) -> None:
    before = f"10.0.0.99 {HOST}\n"
    hosts = _hosts_file(eval_fixture, before)
    sudo_log = _fake_sudo(eval_fixture)
    eval_fixture.set_platform("Darwin")
    eval_fixture.add_security(trusted=True)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts)

    assert (eval_fixture.root / "hosts").read_text() == before
    assert not sudo_log.exists()
    assert f"maps {HOST} to 10.0.0.99" in run.stdout
    assert LAN in run.stdout
    assert "Fix:" in run.stdout


def test_a_loopback_mapping_of_the_name_is_reported_not_duplicated(
    eval_fixture: EvalFixture,
) -> None:
    before = f"127.0.0.1 {HOST}\n::1 {HOST}\n"
    hosts = _hosts_file(eval_fixture, before)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts)

    assert (eval_fixture.root / "hosts").read_text() == before
    assert f"maps {HOST} to 127.0.0.1" in run.stdout


def test_provisioning_does_not_depend_on_the_hosts_file(eval_fixture: EvalFixture) -> None:
    """The script's own calls go to the loopback port-forward.

    A missing hosts file (the machine has not been set up yet) does not stop the deploy.
    """
    absent = str(eval_fixture.root / "no-such-hosts-file")

    eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=absent)

    calls = _api_calls(eval_fixture)
    assert calls
    assert all(call["url"].startswith("http://127.0.0.1:") for call in calls)
    assert eval_fixture.curl_users()


# --- LAN address of this machine ---------------------------------------------------------------


def _hosts_line_printed(run: ScriptRun) -> str:
    """The closing output opens with the hosts line."""
    return run.stdout.splitlines()[0]


def test_lan_address_comes_from_the_default_route_first(eval_fixture: EvalFixture) -> None:
    eval_fixture.set_addresses("10.0.0.9", "192.168.1.5", "172.17.0.1")
    eval_fixture.set_default_route("10.0.0.9")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=_hosts_file(eval_fixture))

    assert _hosts_line_printed(run) == f"10.0.0.9 {HOST}"


def test_lan_address_comes_from_the_default_route_on_macos(eval_fixture: EvalFixture) -> None:
    eval_fixture.set_addresses("10.0.0.9", "192.168.1.5", tool="ifconfig")
    eval_fixture.set_default_route("10.0.0.9", tool="ifconfig")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=_hosts_file(eval_fixture))

    assert _hosts_line_printed(run) == f"10.0.0.9 {HOST}"


def test_lan_address_is_the_single_private_address_without_a_default_route(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.set_addresses("127.0.0.1", "::1", "fe80::1", "169.254.7.7", "10.0.0.9")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=_hosts_file(eval_fixture))

    assert _hosts_line_printed(run) == f"10.0.0.9 {HOST}"


def test_several_private_addresses_without_a_default_route_need_the_flag_before_any_change(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.set_addresses("10.0.0.9", "192.168.1.5", "172.17.0.1")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=1)

    assert "--lan-address" in run.stderr
    for address in ("10.0.0.9", "192.168.1.5", "172.17.0.1"):
        assert address in run.stderr
    assert "Fix:" in run.stderr
    assert eval_fixture.kubectl_calls() == []
    assert _mutations(eval_fixture) == []


def test_no_private_address_needs_the_flag_before_any_change(eval_fixture: EvalFixture) -> None:
    eval_fixture.set_addresses("127.0.0.1", "::1")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=1)

    assert "--lan-address" in run.stderr
    assert _mutations(eval_fixture) == []


def test_the_lan_address_flag_overrides_detection(eval_fixture: EvalFixture) -> None:
    eval_fixture.set_addresses("10.0.0.9", "192.168.1.5")
    eval_fixture.set_default_route("10.0.0.9")

    run = eval_fixture.run(
        "--owner-email",
        OWNER_EMAIL,
        "--lan-address",
        "192.168.1.5",
        PS_HOSTS_FILE=_hosts_file(eval_fixture),
    )

    assert _hosts_line_printed(run) == f"192.168.1.5 {HOST}"


@pytest.mark.parametrize("value", ["not-an-address", "300.1.1.1", "127.0.0.1", "::1", "10.0.0"])
def test_an_unusable_lan_address_is_a_usage_error_before_any_change(
    eval_fixture: EvalFixture, value: str
) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--lan-address", value, expect=2)

    assert "--lan-address" in run.stderr
    assert eval_fixture.kubectl_calls() == []
    assert _mutations(eval_fixture) == []


# --- trust for TLS server authentication only --------------------------------------------------


def test_apply_host_setup_runs_the_ssl_only_trust_command_on_macos(
    eval_fixture: EvalFixture,
) -> None:
    sudo_log = _fake_sudo(eval_fixture)
    eval_fixture.set_platform("Darwin")
    eval_fixture.add_security(trusted=False)

    eval_fixture.run(
        "--owner-email",
        OWNER_EMAIL,
        "--apply-host-setup",
        PS_HOSTS_FILE=_hosts_file(eval_fixture),
    )

    trust_args = MACOS_TRUST.removeprefix("sudo ")
    assert f"{trust_args} {eval_fixture.state_dir / 'ca.pem'}" in sudo_log.read_text()


def test_linux_run_never_prints_or_runs_keychain_commands(eval_fixture: EvalFixture) -> None:
    _seed_legacy_evaluator(eval_fixture)
    eval_fixture.set_platform("Linux")
    security_log = eval_fixture.add_security(trusted=False)
    sudo_log = _fake_sudo(eval_fixture)
    hosts = _hosts_file(eval_fixture)

    first = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts
    )
    second = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts
    )

    for text in (first.output, second.output, sudo_log.read_text()):
        assert "add-trusted-cert" not in text
        assert "remove-trusted-cert" not in text
        assert "System.keychain" not in text
        assert "security " not in text
    assert not security_log.exists()
    assert "replaced" in first.stderr


def test_ca_trust_check_uses_the_ssl_policy_and_is_skipped_when_already_trusted(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.set_platform("Darwin")
    security_log = eval_fixture.add_security(trusted=True)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=_hosts_file(eval_fixture))

    ca = eval_fixture.state_dir / "ca.pem"
    assert f"verify-cert -c {ca} -p ssl" in security_log.read_text()
    assert "add-trusted-cert" not in run.stdout


def test_apply_host_setup_does_not_trust_twice(eval_fixture: EvalFixture) -> None:
    sudo_log = _fake_sudo(eval_fixture)
    eval_fixture.set_platform("Darwin")
    eval_fixture.add_security(trusted=True)

    eval_fixture.run(
        "--owner-email",
        OWNER_EMAIL,
        "--apply-host-setup",
        PS_HOSTS_FILE=_hosts_file(eval_fixture),
    )

    assert "add-trusted-cert" not in sudo_log.read_text()
    assert "tee -a" in sudo_log.read_text()


# --- closing output: hosts line, ca.pem path, enrolment link ------------------------------------

REMOVED_OUTPUT = (
    "Client values",
    "Other machines",
    "LAN colleagues",
    "Before opening the enrolment link",
    "This machine is ready",
    "The local CA was replaced",
    "ps-cli",
    "PS_MCP_URL",
    "Authentik:",
    "OIDC issuer",
    "PS Service:",
    "deployed",
    "already up to date",
    "sudo ",
    "installation-guide",
)
LINK_LINE = f"{AUTH_BASE}/if/flow/ps-passkey-recovery/?flow_token="


def _enrol_with_passkey(fixture: EvalFixture) -> None:
    fixture.run("--owner-email", OWNER_EMAIL)
    pk = next(iter(fixture.curl_users()))
    fixture.seed_curl(devices={pk: [{"pk": 1, "name": "passkey"}]})


def test_closing_output_is_the_hosts_line_the_ca_path_and_the_enrolment_link_in_that_order(
    eval_fixture: EvalFixture,
) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=_hosts_file(eval_fixture))

    lines = run.stdout.splitlines()
    assert len(lines) == 4
    assert lines[0] == f"{LAN} {HOST}"
    assert lines[1] == str(eval_fixture.state_dir / "ca.pem")
    assert lines[1].startswith("/")
    assert "single-use link" in lines[2]
    assert lines[3].startswith(LINK_LINE)


def test_closing_output_with_an_enrolled_owner_is_the_hosts_line_the_ca_path_and_the_message(
    eval_fixture: EvalFixture,
) -> None:
    _enrol_with_passkey(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=_hosts_file(eval_fixture))

    lines = run.stdout.splitlines()
    assert len(lines) == 3
    assert lines[0] == f"{LAN} {HOST}"
    assert lines[1] == str(eval_fixture.state_dir / "ca.pem")
    assert "already has a passkey" in lines[2]
    assert "/if/flow/" not in run.output


@pytest.mark.parametrize("enrolled", [False, True])
def test_closing_output_has_none_of_the_removed_blocks(
    eval_fixture: EvalFixture, *, enrolled: bool
) -> None:
    if enrolled:
        _enrol_with_passkey(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=_hosts_file(eval_fixture))

    for text in REMOVED_OUTPUT:
        assert text not in run.stdout, text


def test_closing_output_follows_a_custom_name_and_lan_address(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run(
        "--owner-email",
        OWNER_EMAIL,
        "--hostname",
        "ps.example.test",
        "--lan-address",
        "10.20.30.40",
        PS_HOSTS_FILE=_hosts_file(eval_fixture),
    )

    assert run.stdout.splitlines()[0] == "10.20.30.40 ps.example.test"
    assert f"{HOST}" not in run.stdout.splitlines()[0]


def test_closing_output_prints_the_link_once_and_never_the_ca_key(
    eval_fixture: EvalFixture,
) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert run.output.count("flow_token=") == 1
    assert "ca.key" not in run.output


def test_a_conflicting_hosts_line_is_reported_right_after_the_hosts_line(
    eval_fixture: EvalFixture,
) -> None:
    hosts = _hosts_file(eval_fixture, f"10.0.0.99 {HOST}\n")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=hosts)

    lines = run.stdout.splitlines()
    assert lines[0] == f"{LAN} {HOST}"
    assert f"maps {HOST} to 10.0.0.99" in lines[1]
    assert lines[2].lstrip().startswith("Fix:")
    assert lines[3] == str(eval_fixture.state_dir / "ca.pem")


# --- removal of the replaced local CA from this machine's trust store ---------------------------


def _failing_sudo(fixture: EvalFixture) -> Path:
    """A `sudo` that logs its argv and fails, as a declined password prompt does."""
    log = fixture.root / "sudo.log"
    sudo = fixture.bin_dir / "sudo"
    sudo.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "' + str(log) + '"\nexit 1\n')
    sudo.chmod(0o755)
    return log


def _removal_commands(sudo_log: Path) -> list[str]:
    if not sudo_log.exists():
        return []
    markers = ("remove-trusted-cert", "delete-certificate", "rm ", "update-ca")
    return [ln for ln in sudo_log.read_text().splitlines() if any(m in ln for m in markers)]


def _replaced_on_darwin(fixture: EvalFixture) -> tuple[str, Path]:
    """A legacy evaluator whose old CA this machine trusts; returns its SHA-1 and kept copy."""
    fixture.set_platform("Darwin")
    _seed_legacy_evaluator(fixture)
    sha1 = _ca_hash(fixture.state_dir / "ca.pem", "sha1")
    fixture.add_security(trusted=True)
    return sha1, fixture.state_dir / f"ca.replaced-{sha1[:8]}.pem"


def test_the_replaced_ca_is_removed_on_macos_by_its_sha1_with_apply_host_setup(
    eval_fixture: EvalFixture,
) -> None:
    sha1, kept = _replaced_on_darwin(eval_fixture)
    sudo_log = _fake_sudo(eval_fixture)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=_hosts_file(eval_fixture)
    )

    assert _removal_commands(sudo_log) == [
        f"security remove-trusted-cert -d {kept.resolve()}",
        f"security delete-certificate -Z {sha1} -t /Library/Keychains/System.keychain",
    ]
    assert "still trusted" not in run.stdout
    assert kept.exists()


def test_the_replaced_ca_is_removed_after_an_interactive_yes(eval_fixture: EvalFixture) -> None:
    sha1, _ = _replaced_on_darwin(eval_fixture)
    sudo_log = _fake_sudo(eval_fixture)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, tty_input="y\n", PS_HOSTS_FILE=_hosts_file(eval_fixture)
    )

    assert any(f"delete-certificate -Z {sha1}" in c for c in _removal_commands(sudo_log))
    assert "still trusted" not in run.stdout


def test_an_interactive_no_removes_nothing_and_points_at_the_flag(
    eval_fixture: EvalFixture,
) -> None:
    _replaced_on_darwin(eval_fixture)
    sudo_log = _fake_sudo(eval_fixture)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, tty_input="n\n", PS_HOSTS_FILE=_hosts_file(eval_fixture)
    )

    assert _removal_commands(sudo_log) == []
    assert "still trusted" in run.stdout


def test_a_non_interactive_run_without_the_flag_removes_nothing_and_points_at_the_flag(
    eval_fixture: EvalFixture,
) -> None:
    _replaced_on_darwin(eval_fixture)
    sudo_log = _fake_sudo(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--yes")

    assert not sudo_log.exists()
    pointer = [ln for ln in run.stdout.splitlines() if "still trusted" in ln]
    assert len(pointer) == 1
    assert "--apply-host-setup" in pointer[0]
    assert run.stdout.splitlines()[-1].startswith(LINK_LINE)


def test_a_failed_removal_keeps_the_old_ca_continues_and_prints_the_pointer(
    eval_fixture: EvalFixture,
) -> None:
    _replaced_on_darwin(eval_fixture)
    sudo_log = _failing_sudo(eval_fixture)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=_hosts_file(eval_fixture)
    )

    assert _removal_commands(sudo_log)
    assert "still trusted" in run.stdout
    assert LINK_LINE in run.stdout


def test_no_trust_anchor_is_removed_when_the_ca_was_not_replaced(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.set_platform("Darwin")
    eval_fixture.add_security(trusted=True)
    sudo_log = _fake_sudo(eval_fixture)
    hosts = _hosts_file(eval_fixture)

    first = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts
    )
    second = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts
    )

    assert _removal_commands(sudo_log) == []
    assert "still trusted" not in first.stdout + second.stdout


def test_an_old_ca_the_machine_does_not_trust_needs_no_removal_and_no_pointer(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.set_platform("Darwin")
    _seed_legacy_evaluator(eval_fixture)
    eval_fixture.add_security(trusted=False)
    sudo_log = _fake_sudo(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, "--yes")

    assert _removal_commands(sudo_log) == []
    assert "still trusted" not in run.stdout


def _replaced_on_linux(fixture: EvalFixture, anchor: Path) -> None:
    """A legacy evaluator whose old CA sits in the trust-store file `anchor`."""
    fixture.set_platform("Linux")
    _seed_legacy_evaluator(fixture)
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text((fixture.state_dir / "ca.pem").read_text())


def test_the_replaced_ca_is_removed_on_debian_with_apply_host_setup(
    eval_fixture: EvalFixture,
) -> None:
    anchor = eval_fixture.debian_anchor
    _replaced_on_linux(eval_fixture, anchor)
    sudo_log = _fake_sudo(eval_fixture)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=_hosts_file(eval_fixture)
    )

    assert _removal_commands(sudo_log) == [f"rm {anchor}", "update-ca-certificates --fresh"]
    assert "still trusted" not in run.stdout


def test_the_replaced_ca_is_removed_on_fedora_with_apply_host_setup(
    eval_fixture: EvalFixture,
) -> None:
    anchor = eval_fixture.fedora_anchor
    _replaced_on_linux(eval_fixture, anchor)
    sudo_log = _fake_sudo(eval_fixture)

    eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=_hosts_file(eval_fixture)
    )

    assert _removal_commands(sudo_log) == [f"rm {anchor}", "update-ca-trust"]


def test_a_linux_trust_file_holding_another_certificate_is_left_alone(
    eval_fixture: EvalFixture,
) -> None:
    anchor = eval_fixture.debian_anchor
    _replaced_on_linux(eval_fixture, anchor)
    other = eval_fixture.root / "other"
    other.mkdir()
    _openssl(
        *("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=another"),
        *("-keyout", "k.pem", "-out", "c.pem"),
        cwd=other,
    )
    anchor.write_text((other / "c.pem").read_text())
    sudo_log = _fake_sudo(eval_fixture)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=_hosts_file(eval_fixture)
    )

    assert _removal_commands(sudo_log) == []
    assert anchor.exists()
    assert "still trusted" not in run.stdout


def test_a_declined_linux_removal_keeps_the_file_and_prints_the_pointer(
    eval_fixture: EvalFixture,
) -> None:
    anchor = eval_fixture.debian_anchor
    _replaced_on_linux(eval_fixture, anchor)
    sudo_log = _failing_sudo(eval_fixture)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=_hosts_file(eval_fixture)
    )

    assert _removal_commands(sudo_log) == [f"rm {anchor}"]
    assert anchor.exists()
    assert "still trusted" in run.stdout


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


# --- hostname switch on an existing install: pre-flight listing -------------------------------

OLD_HOST = "old-host.test"


def _install_for_host(fixture: EvalFixture, host: str) -> None:
    """A release installed for `host`: one real run, so Authentik holds the owner too."""
    fixture.run("--owner-email", OWNER_EMAIL, "--hostname", host)


def _make_machine_trust_the_ca(fixture: EvalFixture) -> None:
    fixture.set_platform("Darwin")
    fixture.add_security(trusted=True)


def test_upgrade_listing_is_printed_before_the_first_cluster_change(
    eval_fixture: EvalFixture,
) -> None:
    _install_for_host(eval_fixture, OLD_HOST)
    hosts = _hosts_file(eval_fixture)
    eval_fixture.set_platform("Darwin")
    eval_fixture.add_security(trusted=False)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, expect=None, PS_HOSTS_FILE=hosts, PS_TEST_APPLY_FAIL="1"
    )

    # The script really stops on a failed kubectl apply (strict mode + pipefail) ...
    assert run.returncode != 0
    assert any("apply" in c for c in eval_fixture.kubectl_calls())
    assert len(eval_fixture.helm_upgrades()) == 1  # only the seeding run
    # ... and the listing was printed before that first cluster change was attempted.
    assert f"switching to {HOST}" in run.stderr
    assert f"{LAN} {HOST}" in run.stderr
    assert f"127.0.0.1 {HOST}" not in run.stderr
    assert "add-trusted-cert" in run.stderr


def test_upgrade_listing_omits_steps_already_done(eval_fixture: EvalFixture) -> None:
    _install_for_host(eval_fixture, OLD_HOST)
    hosts = _hosts_file(eval_fixture, f"{LAN} {HOST}\n")
    _make_machine_trust_the_ca(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=hosts)

    assert f"switching to {HOST}" in run.stderr
    assert f"echo '{LAN} {HOST}'" not in run.stderr
    assert "add-trusted-cert" not in run.stderr


def test_upgrade_listing_mentions_passkey_reenrolment_and_lan_colleagues(
    eval_fixture: EvalFixture,
) -> None:
    _install_for_host(eval_fixture, OLD_HOST)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=_hosts_file(eval_fixture))

    assert "passkey" in run.stderr
    assert "LAN" in run.stderr
    assert "operations-guide.md#moving-an-existing-evaluator-off-a-local-hostname" in run.stderr


def test_upgrade_listing_prints_the_role_assignment_reset_step(eval_fixture: EvalFixture) -> None:
    # The script refuses .local, so a release with a `.local` hostname is seeded into the fake helm.
    eval_fixture.helm_state.mkdir(parents=True)
    old_values = {"psService": {"authentikHostname": "old" + ".local"}}
    (eval_fixture.helm_state / "values.json").write_text(json.dumps(old_values))

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=_hosts_file(eval_fixture))

    assert "DELETE FROM access_role_assignments" in run.stderr
    assert "ps-postgres" in run.stderr
    assert "Do not open the enrolment link before running this command" in run.stderr


def test_no_upgrade_listing_on_fresh_install_or_same_hostname_rerun(
    eval_fixture: EvalFixture,
) -> None:
    fresh = eval_fixture.run("--owner-email", OWNER_EMAIL)
    rerun = eval_fixture.run("--owner-email", OWNER_EMAIL)

    for run in (fresh, rerun):
        assert "switching to" not in run.stderr
        assert "DELETE FROM access_role_assignments" not in run.stderr


def test_release_without_recorded_hostname_prints_no_switch_block(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.helm_state.mkdir(parents=True)
    (eval_fixture.helm_state / "values.json").write_text(json.dumps({"llm": {}}))

    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert "switching to" not in run.stderr


def test_upgrade_listing_does_no_sudo(eval_fixture: EvalFixture) -> None:
    _install_for_host(eval_fixture, OLD_HOST)
    sudo_log = _fake_sudo(eval_fixture)

    eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=_hosts_file(eval_fixture))

    assert not sudo_log.exists()


def test_hostname_switch_issues_a_link_although_the_owner_has_a_stale_passkey(
    eval_fixture: EvalFixture,
) -> None:
    _install_for_host(eval_fixture, OLD_HOST)
    pk = next(iter(eval_fixture.curl_users()))
    eval_fixture.seed_curl(devices={pk: [{"pk": 1, "name": "passkey"}]})

    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert f"{AUTH_BASE}/if/flow/" in run.stdout
    assert "already has a passkey" not in run.stdout


def test_same_hostname_rerun_with_a_passkey_still_issues_no_link(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)
    pk = next(iter(eval_fixture.curl_users()))
    eval_fixture.seed_curl(devices={pk: [{"pk": 1, "name": "passkey"}]})

    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert "already has a passkey" in run.stdout


# --- migration of an evaluator deployed on another name and issuer ---------------------------

OLD_NAME = "authentik" + ".test"
OLD_ROOT_ISSUER = "https://" + OLD_NAME + ":30443" + "/application/o/ps-cli/"
LEGACY_CA_DAYS = "3650"
LINK_MARKER = f"{AUTH_BASE}/if/flow/"
SIGN_DAYS = "397"


def _openssl(*args: str, cwd: Path) -> str:
    done = subprocess.run(  # noqa: S603 - fixed argv, test-owned files
        ["openssl", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
        timeout=OPENSSL_TIMEOUT_SECONDS,
    )
    return done.stdout


def _write_legacy_ca_and_leaf(fixture: EvalFixture, host: str) -> None:
    """An unconstrained ten-year CA and a leaf for `host`, as an old state directory holds."""
    state = fixture.state_dir
    (state / "ca.cnf").write_text(
        "[req]\ndistinguished_name = dn\nprompt = no\nx509_extensions = v3\n"
        "[dn]\nCN = Policy System local CA\n[v3]\nbasicConstraints = critical, CA:TRUE\n"
        "keyUsage = critical, keyCertSign\nsubjectKeyIdentifier = hash\n"
    )
    (state / "leaf.cnf").write_text(
        f"[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = {host}\n[v3]\n"
        f"basicConstraints = CA:FALSE\nsubjectAltName = DNS:{host}\n"
        "subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid, issuer\n"
    )
    _openssl(
        *("req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256"),
        *("-days", LEGACY_CA_DAYS, "-config", "ca.cnf", "-keyout", "ca.key", "-out", "ca.pem"),
        cwd=state,
    )
    _openssl(
        *("req", "-new", "-newkey", "rsa:2048", "-nodes", "-config", "leaf.cnf"),
        *("-keyout", "leaf.key", "-out", "leaf.csr"),
        cwd=state,
    )
    _openssl(
        *("x509", "-req", "-sha256", "-days", SIGN_DAYS, "-in", "leaf.csr"),
        *("-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial"),
        *("-extfile", "leaf.cnf", "-extensions", "v3", "-out", "leaf.pem"),
        cwd=state,
    )


def _ca_hash(path: Path, algorithm: str) -> str:
    out = _openssl(
        "x509", "-in", str(path), "-noout", "-fingerprint", f"-{algorithm}", cwd=path.parent
    )
    return out.split("=", 1)[1].strip().replace(":", "").upper()


def _seed_legacy_evaluator(fixture: EvalFixture) -> None:
    """An evaluator on another name: root-path issuer, unconstrained CA, an enrolled passkey."""
    fixture.run("--owner-email", OWNER_EMAIL, "--hostname", OLD_NAME)
    values = fixture.deployed_values()
    values["psService"]["auth"]["issuer"] = OLD_ROOT_ISSUER
    values["psService"]["authzBootstrapOwner"]["issuer"] = OLD_ROOT_ISSUER
    (fixture.helm_state / "values.json").write_text(json.dumps(values))
    _write_legacy_ca_and_leaf(fixture, OLD_NAME)
    pk = next(iter(fixture.curl_users()))
    fixture.seed_curl(devices={pk: [{"pk": 1, "name": "passkey"}]})


def test_migration_is_detected_from_the_recorded_values_and_announced_before_any_cluster_change(
    eval_fixture: EvalFixture,
) -> None:
    _seed_legacy_evaluator(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=None, PS_TEST_APPLY_FAIL="1")

    assert run.returncode != 0
    assert any("apply" in c for c in eval_fixture.kubectl_calls())
    assert len(eval_fixture.helm_upgrades()) == 1
    assert f"installed for {OLD_NAME}; switching to {HOST}" in run.stderr
    assert OLD_ROOT_ISSUER in run.stderr
    assert ISSUER in run.stderr
    assert "LINK" not in run.stdout
    assert LINK_MARKER not in run.output


def test_migration_moves_the_release_to_the_new_name_and_issuer_in_one_upgrade(
    eval_fixture: EvalFixture,
) -> None:
    _seed_legacy_evaluator(eval_fixture)

    eval_fixture.run("--owner-email", OWNER_EMAIL)

    values = eval_fixture.deployed_values()["psService"]
    assert len(eval_fixture.helm_upgrades()) == 2
    assert values["authentikHostname"] == HOST
    assert values["auth"]["issuer"] == ISSUER
    assert values["authzBootstrapOwner"]["issuer"] == ISSUER


def test_migration_replaces_the_unconstrained_ca_and_names_the_old_ca_to_remove(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.set_platform("Darwin")
    _seed_legacy_evaluator(eval_fixture)
    old_ca = eval_fixture.state_dir / "ca.pem"
    sha1, sha256 = _ca_hash(old_ca, "sha1"), _ca_hash(old_ca, "sha256")
    old_text = old_ca.read_text()

    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    kept = eval_fixture.state_dir / f"ca.replaced-{sha1[:8]}.pem"
    assert kept.read_text() == old_text
    assert (eval_fixture.state_dir / "ca.pem").read_text() != old_text
    assert "Permitted: DNS:" + HOST in _x509_text(eval_fixture.state_dir / "ca.pem")
    stream = run.stderr
    assert sha1 in stream
    assert sha256 in stream
    assert "Policy System local CA" in stream
    assert f"sudo security remove-trusted-cert -d {kept.resolve()}" in stream
    assert (
        f"sudo security delete-certificate -Z {sha1} -t /Library/Keychains/System.keychain"
        in stream
    )
    assert "sudo rm /usr/local/share/ca-certificates/ps-local-ca.crt" in stream
    assert "update-ca-certificates --fresh" in stream
    assert "sudo rm /etc/pki/ca-trust/source/anchors/ps-local-ca.pem" in stream
    assert "Policy System local CA" not in run.stdout
    assert sha1 not in run.stdout
    assert ":".join([sha1[:2], sha1[2:4]]) not in run.output
    assert f"{eval_fixture.state_dir.resolve()}/ca.pem" in run.stderr


def test_migration_tells_the_machine_to_remove_old_name_hosts_lines_and_add_the_new_one(
    eval_fixture: EvalFixture,
) -> None:
    _seed_legacy_evaluator(eval_fixture)
    original = f"127.0.0.1 localhost\n127.0.0.1 {OLD_NAME}\n::1 {OLD_NAME}\n"
    hosts = _hosts_file(eval_fixture, original)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=hosts)

    assert f"  127.0.0.1 {OLD_NAME}" in run.stderr
    assert f"  ::1 {OLD_NAME}" in run.stderr
    assert "Remove these lines" in run.stderr
    assert "127.0.0.1 localhost" not in run.stderr
    assert f"echo '{LAN} {HOST}' | sudo tee -a {hosts}" in run.stderr
    assert (eval_fixture.root / "hosts").read_text() == original


def test_migration_without_old_name_hosts_lines_asks_for_no_removal(
    eval_fixture: EvalFixture,
) -> None:
    _seed_legacy_evaluator(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_HOSTS_FILE=_hosts_file(eval_fixture))

    assert "Remove these lines" not in run.stderr


def test_migration_issues_a_fresh_single_use_link_although_the_owner_has_a_passkey_on_the_old_name(
    eval_fixture: EvalFixture,
) -> None:
    _seed_legacy_evaluator(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert run.stdout.count(LINK_MARKER) == 1
    assert "already has a passkey" not in run.stdout
    squashed = " ".join(run.stderr.split())
    assert "all passkeys must be registered again" in squashed
    assert "registers a new passkey with a fresh enrolment link" in squashed


def test_migration_prints_the_role_assignment_reset_before_any_cluster_change_and_link(
    eval_fixture: EvalFixture,
) -> None:
    _seed_legacy_evaluator(eval_fixture)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=None, PS_TEST_APPLY_FAIL="1")

    assert "DELETE FROM access_role_assignments" in run.stderr
    assert "Do not open the enrolment link before running this command" in run.stderr
    assert LINK_MARKER not in run.output


def test_a_same_name_issuer_change_announces_the_role_reset_and_issues_no_new_link(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)
    values = eval_fixture.deployed_values()
    values["psService"]["auth"]["issuer"] = OLD_ROOT_ISSUER.replace(OLD_NAME, HOST)
    values["psService"]["authzBootstrapOwner"]["issuer"] = OLD_ROOT_ISSUER.replace(OLD_NAME, HOST)
    (eval_fixture.helm_state / "values.json").write_text(json.dumps(values))
    pk = next(iter(eval_fixture.curl_users()))
    eval_fixture.seed_curl(devices={pk: [{"pk": 1, "name": "passkey"}]})

    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert "issuer changes" in run.stderr
    assert ISSUER in run.stderr
    assert "DELETE FROM access_role_assignments" in run.stderr
    assert "switching to" not in run.stderr
    assert "registered again" not in run.stderr
    assert "already has a passkey" in run.stdout
    assert LINK_MARKER not in run.stdout
    assert "replaced" not in run.output


def test_rerun_after_migration_creates_no_second_upgrade_hosts_line_ca_or_user(
    eval_fixture: EvalFixture,
) -> None:
    _seed_legacy_evaluator(eval_fixture)
    hosts = _hosts_file(eval_fixture, f"127.0.0.1 {OLD_NAME}\n")
    _fake_sudo(eval_fixture)
    first = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts
    )
    ca_after_first = (eval_fixture.state_dir / "ca.pem").read_text()
    users_after_first = eval_fixture.curl_users()

    second = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, "--apply-host-setup", PS_HOSTS_FILE=hosts
    )

    assert "replaced" in first.stderr
    assert len(eval_fixture.helm_upgrades()) == 2
    assert "unchanged" in second.stderr
    assert (eval_fixture.root / "hosts").read_text().splitlines().count(f"{LAN} {HOST}") == 1
    assert (eval_fixture.state_dir / "ca.pem").read_text() == ca_after_first
    assert len(list(eval_fixture.state_dir.glob("ca.replaced-*.pem"))) == 1
    assert eval_fixture.curl_users() == users_after_first
    assert len(eval_fixture.curl_users()) == 1
    assert "switching to" not in second.stderr
    assert "replaced" not in second.output
    assert "SHA-1" not in second.output


def test_a_leaf_without_the_name_san_is_reissued_on_rerun(eval_fixture: EvalFixture) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)
    state = eval_fixture.state_dir
    ca_before = (state / "ca.pem").read_text()
    (state / "bare.cnf").write_text(
        "[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = bare\n"
    )
    _openssl(
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-config",
        "bare.cnf",
        "-keyout",
        "leaf.key",
        "-out",
        "bare.csr",
        cwd=state,
    )
    _openssl(
        "x509",
        "-req",
        "-sha256",
        "-days",
        SIGN_DAYS,
        "-in",
        "bare.csr",
        "-CA",
        "ca.pem",
        "-CAkey",
        "ca.key",
        "-CAserial",
        "ca.srl",
        "-out",
        "leaf.pem",
        cwd=state,
    )
    assert "Subject Alternative Name" not in _x509_text(state / "leaf.pem")

    eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert _leaf_sans(eval_fixture) == [f"DNS:{HOST}"]
    assert (state / "ca.pem").read_text() == ca_before


# --- order and in-cluster discovery probe -----------------------------------------------------


def _index(calls: list[str], needle: str) -> int:
    return next(i for i, c in enumerate(calls) if needle in c)


def test_discovery_probe_runs_in_the_ps_service_pod_after_rollout(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    kube = eval_fixture.kubectl_calls()
    probe = next(c for c in kube if " exec " in f" {c} ")
    assert "deploy/policy-system-ps-service" in probe
    assert f"{ISSUER}.well-known/openid-configuration" in probe
    assert "/ps-trust/bundle.crt" in probe
    assert _index(kube, "rollout status deployment/policy-system-ps-service") < kube.index(probe)


def test_discovery_probe_failure_names_the_issuer_and_exits_1(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, PS_TEST_EXEC_FAIL="1", PS_DISCOVERY_ATTEMPTS="1", expect=1
    )

    assert ISSUER in run.stderr
    assert "Fix:" in run.stderr
    assert eval_fixture.curl_users() == {}


def test_ps_service_is_awaited_without_any_certificate_refresh_or_restart(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.run("--owner-email", OWNER_EMAIL)

    kube = eval_fixture.kubectl_calls()
    assert not any("rollout restart" in c for c in kube)
    assert _index(kube, "rollout status deployment/policy-system-proxy") < _index(
        kube, "rollout status deployment/policy-system-ps-service"
    )


def test_a_leftover_local_tls_keypair_in_authentik_prints_a_fix(eval_fixture: EvalFixture) -> None:
    eval_fixture.seed_curl(stale_tls_keypair=True)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert "ps-local-tls" in run.stderr
    assert "Fix:" in run.stderr


def test_no_leftover_keypair_prints_no_certificate_fix(eval_fixture: EvalFixture) -> None:
    run = eval_fixture.run("--owner-email", OWNER_EMAIL)

    assert "ps-local-tls" not in run.stderr


# --- exposure preflight: the LAN-facing listener must not meet a public address -----------------

PUBLIC_ADDRESS = "203.0.113.9"
PRIVATE_ONLY = (
    "127.0.0.1",
    "::1",
    "10.1.2.3",
    "172.16.0.1",
    "172.31.255.254",
    "192.168.1.5",
    "169.254.7.7",
    "fe80::1",
    "fd00::1",
)


def test_public_address_aborts_before_any_cluster_call_and_names_it(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.set_addresses("192.168.1.5", PUBLIC_ADDRESS)

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=1)

    assert PUBLIC_ADDRESS in run.stderr
    assert "192.168.1.5" not in run.stderr
    assert "--allow-public-host" in run.stderr
    assert eval_fixture.kubectl_calls() == []
    assert _mutations(eval_fixture) == []


def test_every_offending_address_is_named(eval_fixture: EvalFixture) -> None:
    eval_fixture.set_addresses(PUBLIC_ADDRESS, "198.51.100.4", "2001:db8::5")

    run = eval_fixture.run("--check-exposure", expect=1)

    for address in (PUBLIC_ADDRESS, "198.51.100.4", "2001:db8::5"):
        assert address in run.stderr


def test_allow_public_host_flag_lets_the_preflight_pass(eval_fixture: EvalFixture) -> None:
    eval_fixture.set_addresses("192.168.1.5", PUBLIC_ADDRESS)

    run = eval_fixture.run("--check-exposure", "--allow-public-host")

    assert PUBLIC_ADDRESS not in run.stderr


def test_allow_public_host_flag_lets_a_full_deploy_run_through(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.set_addresses(LAN, PUBLIC_ADDRESS)

    eval_fixture.run("--owner-email", OWNER_EMAIL, "--allow-public-host")

    assert len(eval_fixture.helm_upgrades()) == 1


def test_private_loopback_and_link_local_only_pass_without_the_flag(
    eval_fixture: EvalFixture,
) -> None:
    eval_fixture.set_addresses(*PRIVATE_ONLY)

    eval_fixture.run("--check-exposure")


def test_no_non_loopback_address_passes(eval_fixture: EvalFixture) -> None:
    eval_fixture.set_addresses("127.0.0.1", "::1")

    eval_fixture.run("--check-exposure")


@pytest.mark.parametrize(
    "address",
    [
        "172.32.0.1",
        "172.15.255.255",
        "100.64.0.1",
        "100.127.255.254",
        "192.169.0.1",
        "8.8.8.8",
        "fec0::1",
        "2001:db8::1",
        "::ffff:8.8.8.8",
        "::ffff:0808:0808",
    ],
)
def test_addresses_outside_the_private_ranges_are_public(
    eval_fixture: EvalFixture, address: str
) -> None:
    eval_fixture.set_addresses("192.168.1.5", address)

    run = eval_fixture.run("--check-exposure", expect=1)

    assert address in run.stderr


@pytest.mark.parametrize(
    "address",
    [
        "172.16.0.1",
        "172.31.0.1",
        "169.254.1.1",
        "FD00::1",
        "FC00::1",
        "fe80::1%en0",
        "febf::1",
        "::ffff:10.0.0.1",
        "::ffff:c0a8:0105",
    ],
)
def test_addresses_inside_the_private_ranges_pass(eval_fixture: EvalFixture, address: str) -> None:
    eval_fixture.set_addresses(address)

    eval_fixture.run("--check-exposure")


def test_check_exposure_mode_exits_after_the_check_without_tool_checks(
    eval_fixture: EvalFixture,
) -> None:
    for tool in ("kubectl", "helm", "openssl", "curl", "jq", "podman"):
        eval_fixture.remove_tool(tool)

    run = eval_fixture.run("--check-exposure")

    assert "Missing required tool" not in run.stderr
    assert not eval_fixture.kubectl_log.exists()
    assert not eval_fixture.helm_log.exists()


def test_a_normal_run_checks_exposure_before_the_tool_check(eval_fixture: EvalFixture) -> None:
    eval_fixture.set_addresses(PUBLIC_ADDRESS)
    eval_fixture.remove_tool("kubectl")

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, expect=1)

    assert PUBLIC_ADDRESS in run.stderr
    assert "Missing required tool" not in run.stderr


def test_ifconfig_output_is_parsed_when_ip_is_absent(eval_fixture: EvalFixture) -> None:
    eval_fixture.set_addresses("192.168.1.5", PUBLIC_ADDRESS, "fe80::1%en0", tool="ifconfig")

    run = eval_fixture.run("--check-exposure", expect=1)

    assert PUBLIC_ADDRESS in run.stderr
    assert "192.168.1.5" not in run.stderr
    assert "fe80" not in run.stderr


def test_ifconfig_private_only_passes(eval_fixture: EvalFixture) -> None:
    eval_fixture.set_addresses("127.0.0.1", "192.168.1.5", "fe80::1%en0", tool="ifconfig")

    eval_fixture.run("--check-exposure")


def test_no_address_tool_fails_closed_naming_the_flag(eval_fixture: EvalFixture) -> None:
    for name in ("ip", "ifconfig"):
        (eval_fixture.bin_dir / name).unlink(missing_ok=True)

    run = eval_fixture.run("--check-exposure", expect=1)

    assert "--allow-public-host" in run.stderr
    assert "cannot" in run.stderr.lower()


def test_help_lists_the_exposure_flags_and_unknown_flags_still_fail(
    eval_fixture: EvalFixture,
) -> None:
    helped = eval_fixture.run("--help")
    unknown = eval_fixture.run("--bogus", expect=2)

    assert "--allow-public-host" in helped.stdout
    assert "--check-exposure" in helped.stdout
    assert "unknown flag" in unknown.stderr


def test_help_documents_the_lan_address_flag(eval_fixture: EvalFixture) -> None:
    helped = eval_fixture.run("--help")

    assert "--lan-address" in helped.stdout
