"""Entra CLI app registration + service principal + delegated-permission grant (AC-BI-004
completion; PLAN.md §5/S11). Admin-consent preflight itself is
test_admin_consent_preflight.py's own concern -- see that file for AC-BI-005.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors scripts/deploy-ps.sh's own API_APP_NAME/CLI_APP_NAME literals -- hardcoded here rather
# than imported, same precedent as test_llm_provisioning.py's own SUBSCRIPTION_ID/RESOURCE_GROUP
# constants (conftest.py isn't a runtime-importable module from a test file collected this way).
API_APP_NAME = "Policy System API"
CLI_APP_NAME = "Policy System CLI"


def fake_app_id(display_name: str) -> str:
    """Independently reproduce conftest.py's fake `az ad app create`'s deterministic appId
    ("appid-<slugified display name>") -- same helper as conftest.py's own `fake_app_id`.
    """
    return f"appid-{display_name.lower().replace(' ', '-')}"


def _seed_ready_subscription(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def _app_create_calls(fixture: DeployPsFixture, display_name: str) -> list[str]:
    return [
        line
        for line in fixture.read_az_log()
        if line.startswith("ad app create") and display_name in line
    ]


def test_creates_cli_app_and_service_principal_when_absent(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed_ready_subscription(deploy_ps_fixture)
    expected_cli_app_id = fake_app_id(CLI_APP_NAME)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    create_calls = _app_create_calls(deploy_ps_fixture, CLI_APP_NAME)
    assert len(create_calls) == 1
    assert "--is-fallback-public-client true" in create_calls[0]
    assert (deploy_ps_fixture.azure_state / "ad-apps" / f"{CLI_APP_NAME}.json").exists()
    assert deploy_ps_fixture.read_service_principal_exists(expected_cli_app_id)


def test_adds_delegated_permission_for_the_api_apps_access_as_user_scope(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed_ready_subscription(deploy_ps_fixture)
    expected_api_app_id = fake_app_id(API_APP_NAME)
    expected_cli_app_id = fake_app_id(CLI_APP_NAME)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    scope_id = deploy_ps_fixture.read_api_scope_id(expected_api_app_id)
    assert scope_id is not None

    permission_add_calls = [
        line for line in deploy_ps_fixture.read_az_log() if line.startswith("ad app permission add")
    ]
    assert len(permission_add_calls) == 1
    call = permission_add_calls[0]
    assert f"--id {expected_cli_app_id}" in call
    assert f"--api {expected_api_app_id}" in call
    assert f"--api-permissions {scope_id}=Scope" in call
