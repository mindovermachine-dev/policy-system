"""The experimental notice `scripts/deploy-ps-prod.sh` prints before creating any Azure resource.

The production path is verified on `kind` only, so the script says so up front and points at
the guide's Verification status section. The notice is checked with the fixture's fake `az` and
`kubectl`: declining at the prompt must leave both call logs free of any provisioning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
NOTICE_HEADLINE = "EXPERIMENTAL: this production path is not verified on AKS."
VERIFICATION_STATUS_LINK = "docs/artifacts/installation-guide.md#verification-status"
MUTATING_AZ_PREFIXES = ("group create", "cognitiveservices account create", "aks create")


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)


def test_notice_is_printed_with_a_link_to_the_verification_status(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(stdin="N\n")

    assert NOTICE_HEADLINE in run.stderr
    assert VERIFICATION_STATUS_LINK in run.stderr


def test_notice_precedes_the_confirmation_table(deploy_ps_fixture: DeployPsFixture) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(stdin="N\n")

    assert NOTICE_HEADLINE in run.stderr
    assert "The following Azure resources will be used" in run.stdout


def test_declining_after_the_notice_creates_no_azure_resource_and_calls_no_kubectl(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(stdin="N\n")

    assert NOTICE_HEADLINE in run.stderr
    assert not [
        line for line in deploy_ps_fixture.read_az_log() if line.startswith(MUTATING_AZ_PREFIXES)
    ]
    assert deploy_ps_fixture.read_kubectl_log() == []


def test_notice_is_printed_on_a_confirmed_run_too(deploy_ps_fixture: DeployPsFixture) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes")

    assert NOTICE_HEADLINE in run.stderr
