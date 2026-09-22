"""Entra API app registration + service principal + v2-token PATCH (AC-BI-001 script-half,
AC-BI-004 partial; PLAN.md §5/S10). Own resource type, no `$account_name`-equivalent handoff
from S9 (IMPL_SLICE_9.md's "Next extension point" note) -- mirrors
ps-service/tests/deploy_ps/test_llm_provisioning.py's own test shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors scripts/deploy-ps.sh's own API_APP_NAME/CLI_APP_NAME/ACCESS_AS_USER_SCOPE_VALUE
# literals -- hardcoded here rather than imported, same precedent as
# test_llm_provisioning.py's own SUBSCRIPTION_ID/RESOURCE_GROUP constants (conftest.py isn't a
# runtime-importable module from a test file collected this way -- only its fixtures are).
API_APP_NAME = "Policy System API"
CLI_APP_NAME = "Policy System CLI"
ACCESS_AS_USER_SCOPE_VALUE = "access_as_user"


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


def _sp_create_calls(fixture: DeployPsFixture, app_id: str) -> list[str]:
    return [
        line
        for line in fixture.read_az_log()
        if line.startswith("ad sp create") and f"--id {app_id}" in line
    ]


def test_creates_api_app_and_its_service_principal_when_absent(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed_ready_subscription(deploy_ps_fixture)
    expected_app_id = fake_app_id(API_APP_NAME)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert len(_app_create_calls(deploy_ps_fixture, API_APP_NAME)) == 1
    assert (deploy_ps_fixture.azure_state / "ad-apps" / f"{API_APP_NAME}.json").exists()
    assert deploy_ps_fixture.read_service_principal_exists(expected_app_id)
    assert len(_sp_create_calls(deploy_ps_fixture, expected_app_id)) == 1


def test_existing_api_app_without_service_principal_still_gets_one_created(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-004's actual claim: a create-if-absent app that already exists but is missing only
    its service principal still gets one created -- the exact AADSTS650052 gap
    (docs/artifacts/idp-configuration-contract.md's "Common pitfalls") ensure_api_app_registration
    must close even on a rerun, not just on first creation.
    """
    _seed_ready_subscription(deploy_ps_fixture)
    app_id = deploy_ps_fixture.seed_existing_app(API_APP_NAME)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _app_create_calls(deploy_ps_fixture, API_APP_NAME) == []
    assert deploy_ps_fixture.read_service_principal_exists(app_id)
    assert len(_sp_create_calls(deploy_ps_fixture, app_id)) == 1


def test_rerun_with_existing_api_app_makes_no_ad_app_create_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed_ready_subscription(deploy_ps_fixture)
    app_id = deploy_ps_fixture.seed_existing_app(API_APP_NAME)
    deploy_ps_fixture.seed_service_principal(app_id)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _app_create_calls(deploy_ps_fixture, API_APP_NAME) == []
    assert _sp_create_calls(deploy_ps_fixture, app_id) == []


def test_sets_requested_access_token_version_2_via_rest_patch(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """docs/artifacts/idp-configuration-contract.md's "Common pitfalls": a Graph-API-created app
    registration (what `az ad app create` calls) defaults `api.requestedAccessTokenVersion`
    unset (v1 tokens), unlike the Portal's "Expose an API" wizard. This is the exact PATCH that
    closes that gap for a script-created registration.
    """
    _seed_ready_subscription(deploy_ps_fixture)
    expected_app_id = fake_app_id(API_APP_NAME)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    patch_calls = [
        line for line in deploy_ps_fixture.read_az_log() if line.startswith("rest --method PATCH")
    ]
    assert len(patch_calls) == 1
    assert f"appId='{expected_app_id}'" in patch_calls[0]
    assert '"requestedAccessTokenVersion":2' in patch_calls[0]


def test_creates_the_access_as_user_scope_with_expected_display_and_consent_text(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed_ready_subscription(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    patch_calls = [
        line for line in deploy_ps_fixture.read_az_log() if line.startswith("rest --method PATCH")
    ]
    assert len(patch_calls) == 1
    body = patch_calls[0]
    assert f'"value":"{ACCESS_AS_USER_SCOPE_VALUE}"' in body
    assert '"type":"User"' in body
    assert '"isEnabled":true' in body
    assert '"adminConsentDisplayName":"Access Policy System as the signed-in user"' in body
    assert (
        '"adminConsentDescription":"Allows PS-Cli to call Policy System on behalf of the '
        'signed-in user"' in body
    )
    assert '"userConsentDisplayName":"Access Policy System as the signed-in user"' in body
    assert (
        '"userConsentDescription":"Allows PS-Cli to call Policy System on behalf of the '
        'signed-in user"' in body
    )


def test_insufficient_application_administrator_role_prints_exact_manual_az_commands(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """The signed-in identity lacks Application Administrator: `ad app create` for the API app
    fails, and the script must print the exact copy-pasteable commands a privileged colleague
    needs to run -- both the API and CLI app chains, since neither exists yet at this point.
    """
    _seed_ready_subscription(deploy_ps_fixture)
    deploy_ps_fixture.seed_app_create_denied(API_APP_NAME)

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert (
        f'api_app_id=$(az ad app create --display-name "{API_APP_NAME}" --query appId -o tsv)'
        in run.output
    )
    assert 'az ad sp create --id "$api_app_id"' in run.output
    assert 'az ad app update --id "$api_app_id" --identifier-uris "api://$api_app_id"' in run.output
    assert (
        f'cli_app_id=$(az ad app create --display-name "{CLI_APP_NAME}" '
        "--is-fallback-public-client true --query appId -o tsv)" in run.output
    )
    assert 'az ad sp create --id "$cli_app_id"' in run.output
    assert 'az ad app permission admin-consent --id "$cli_app_id"' in run.output
