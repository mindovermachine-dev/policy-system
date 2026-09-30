"""First-owner creation in `scripts/deploy-ps-prod.sh` (issue #165; AC-BI-002, -004, -014, -016).

The script reaches Authentik's admin API through a loopback `kubectl port-forward` (the admin API
is deliberately NOT on the public Ingress, AC-BI-019) and rewrites the returned enrolment link
onto the public `https://<host>/auth` authority (OD-2). The fake `curl` is the stateful Authentik
API built from responses recorded against a real Authentik 2026.8.3; the fake `kubectl` prints
kubectl's own `Forwarding from ...` line and blocks until killed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from conftest import DeployPsFixture

OWNER_EMAIL = "owner@example.test"
TOKEN = "test-token"
PF_PORT = "45678"
AUTHENTIK_SERVICE = "policy-system-authentik-server"


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def _public_base(fixture: DeployPsFixture) -> str:
    values = fixture.read_helm_release_values()
    assert values is not None
    ps_service = cast("dict[str, dict[str, str]]", values["psService"])
    return ps_service["authentik"]["publicUrl"]


def _link_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if "/if/flow/" in line]


def test_fresh_run_creates_the_owner_through_a_loopback_port_forward(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    users = list(deploy_ps_fixture.read_authentik_users().values())
    assert len(users) == 1
    assert users[0]["username"] == users[0]["email"] == OWNER_EMAIL
    forwards = [c for c in deploy_ps_fixture.read_kubectl_log() if c.startswith("port-forward")]
    assert forwards == [f"port-forward svc/{AUTHENTIK_SERVICE} :80 --address 127.0.0.1"]
    urls = deploy_ps_fixture.read_curl_urls()
    assert urls
    assert all(u.startswith(f"http://127.0.0.1:{PF_PORT}/auth/api/v3/") for u in urls)


def test_authentik_is_awaited_before_the_owner_is_created(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    kubectl = deploy_ps_fixture.read_kubectl_log()
    rollout = next(
        i for i, c in enumerate(kubectl) if f"rollout status deployment/{AUTHENTIK_SERVICE}" in c
    )
    forward = next(i for i, c in enumerate(kubectl) if c.startswith("port-forward"))
    ingress = next(
        i for i, c in enumerate(kubectl) if c == f"apply -f - kind=Ingress name={AUTHENTIK_SERVICE}"
    )
    assert ingress < rollout < forward


def test_the_printed_link_carries_the_public_authority_once(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """OD-2: Authentik builds the link from the request it received (the loopback port-forward);
    the script rewrites scheme and authority onto the public host, without doubling `/auth`.
    """
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    links = _link_lines(run.stdout)
    assert len(links) == 1
    public = _public_base(deploy_ps_fixture)
    assert links[0].startswith(f"{public}/if/flow/ps-passkey-recovery/?flow_token=")
    assert "127.0.0.1" not in links[0]
    assert f"{public}/auth" not in links[0]


def test_rerun_with_an_unenrolled_owner_issues_a_fresh_link_without_a_second_user(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", expect=0)

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert len(_link_lines(run.stdout)) == 1
    assert len(deploy_ps_fixture.read_authentik_users()) == 1


def test_rerun_with_an_enrolled_owner_is_a_true_no_op_for_the_owner(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-004: a passkey on file means no new link and no change to the user."""
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", expect=0)
    pk = next(iter(deploy_ps_fixture.read_authentik_users()))
    deploy_ps_fixture.seed_curl(devices={pk: [{"pk": 1, "name": "passkey"}]})
    before = len(deploy_ps_fixture.read_curl_urls())

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _link_lines(run.stdout) == []
    assert "already has a passkey" in run.stdout
    assert not any("/recovery/" in u for u in deploy_ps_fixture.read_curl_urls()[before:])
    assert len(deploy_ps_fixture.read_authentik_users()) == 1


def test_unreachable_authentik_api_fails_naming_the_fix_and_a_rerun_completes(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-016: no half-created user; the next run finishes the job."""
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_curl(unreachable=True)

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert "Cannot reach the Authentik API" in run.stderr
    assert deploy_ps_fixture.read_authentik_users() == {}
    deploy_ps_fixture.seed_curl(unreachable=False)
    deploy_ps_fixture.run_deploy("--yes", expect=0)
    assert len(deploy_ps_fixture.read_authentik_users()) == 1


def test_a_failing_port_forward_fails_naming_the_fix_and_creates_no_user(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=1, extra_env={"PS_TEST_PF_FAIL": "1"})

    assert "kubectl port-forward" in run.stderr
    assert deploy_ps_fixture.read_authentik_users() == {}


def test_a_rejected_shared_token_prints_the_upgrade_fix_and_creates_nothing(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """F-1: an Authentik tenant older than the shared token answers 403; nothing is created."""
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_curl(token="a-different-token")

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert "does not rotate" in run.stderr
    assert "existingSecret" in run.stderr
    assert deploy_ps_fixture.read_authentik_users() == {}


def test_the_shared_token_never_reaches_output_argv_or_call_logs(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    everything = (
        run.output
        + json.dumps(deploy_ps_fixture.read_curl_calls())
        + "\n".join(deploy_ps_fixture.read_kubectl_log())
        + "\n".join(deploy_ps_fixture.read_helm_log())
        + "\n".join(deploy_ps_fixture.read_az_log())
    )
    assert TOKEN not in everything


def test_owner_lives_in_authentik_only_never_in_an_az_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """No Azure identity or Key Vault call carries the owner (no tenant app registration either,
    issue #129 AC-BI-001).
    """
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", "--owner-email", "other@example.test", expect=0)

    az_log = deploy_ps_fixture.read_az_log()
    assert not any("other@example.test" in line for line in az_log)
    assert not any(line.startswith(("ad app", "ad sp", "ad user")) for line in az_log)
