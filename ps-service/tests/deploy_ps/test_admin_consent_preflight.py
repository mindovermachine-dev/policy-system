"""Admin-consent preflight for the CLI app's delegated permission grant (AC-BI-005; PLAN.md
§5/S11). The headline claim: `az ad app permission list-grants` (an unprivileged read) is
checked *before* `az ad app permission admin-consent` (a privileged write) is ever attempted, so
a rerun after a colleague already granted consent out of band doesn't re-fail the same
privilege-gated call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors scripts/deploy-ps.sh's own CLI_APP_NAME literal -- hardcoded here rather than
# imported, same precedent as test_llm_provisioning.py's own SUBSCRIPTION_ID/RESOURCE_GROUP
# constants (conftest.py isn't a runtime-importable module from a test file collected this way).
CLI_APP_NAME = "Policy System CLI"


def fake_app_id(display_name: str) -> str:
    """Independently reproduce conftest.py's fake `az ad app create`'s deterministic appId
    ("appid-<slugified display name>") -- same helper as conftest.py's own `fake_app_id`.
    """
    return f"appid-{display_name.lower().replace(' ', '-')}"


def _seed_ready_subscription(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def _admin_consent_calls(fixture: DeployPsFixture) -> list[str]:
    return [
        line for line in fixture.read_az_log() if line.startswith("ad app permission admin-consent")
    ]


def _list_grants_calls(fixture: DeployPsFixture) -> list[str]:
    return [
        line for line in fixture.read_az_log() if line.startswith("ad app permission list-grants")
    ]


def test_consent_not_yet_granted_attempts_admin_consent(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed_ready_subscription(deploy_ps_fixture)
    expected_cli_app_id = fake_app_id(CLI_APP_NAME)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert len(_list_grants_calls(deploy_ps_fixture)) == 1
    consent_calls = _admin_consent_calls(deploy_ps_fixture)
    assert len(consent_calls) == 1
    assert f"--id {expected_cli_app_id}" in consent_calls[0]
    assert deploy_ps_fixture.read_consent_granted(expected_cli_app_id)


def test_consent_already_granted_skips_the_write_call_entirely(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-005's exact claim: when `list-grants` already shows `AllPrincipals` consent, the
    privileged `admin-consent` write is never invoked at all -- not attempted-and-ignored, not
    attempted-and-succeeding-again, simply never called.
    """
    _seed_ready_subscription(deploy_ps_fixture)
    deploy_ps_fixture.seed_admin_consent_granted(CLI_APP_NAME)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert len(_list_grants_calls(deploy_ps_fixture)) == 1
    assert _admin_consent_calls(deploy_ps_fixture) == []


def test_non_admin_operator_with_consent_not_yet_granted_prints_exact_manual_command_and_exits(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """The signed-in identity can create/configure app registrations (Application Administrator)
    but can't grant tenant-wide consent (needs Global Administrator / Privileged Role
    Administrator, a step up) -- `admin-consent` itself fails, and the script must print the one
    command a privileged colleague needs to run, then exit with the same controlled
    EXIT_FAILURE=1 every other preflight/business failure in this script uses (not an uncaught
    crash) -- confirmed against spikes/deploy-ps-azure/deploy-ps.sh's own
    print_admin_consent_manual_step, which every prior step (app creation, service principals,
    permission grant) has already completed and is idempotent, so a rerun after the colleague
    grants consent resumes cleanly rather than redoing any of that.
    """
    _seed_ready_subscription(deploy_ps_fixture)
    expected_cli_app_id = deploy_ps_fixture.seed_admin_consent_denied(CLI_APP_NAME)

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert f"az ad app permission admin-consent --id {expected_cli_app_id}" in run.output
    assert "Global Administrator" in run.output
    assert "Privileged Role Administrator" in run.output
    assert not deploy_ps_fixture.read_consent_granted(expected_cli_app_id)
