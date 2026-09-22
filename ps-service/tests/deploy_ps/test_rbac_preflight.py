"""RBAC preflight for `scripts/deploy-ps.sh` (PLAN.md §0.5/§5/S6 -- user-only, no
service-principal branch).

An operator whose signed-in identity has neither `Owner` nor `Contributor` at subscription
scope is stopped before any other Azure resource is touched -- including S7's resource-provider
registration, which runs immediately after this check in `main()` -- with the same actionable
fix-command message shape as `scripts/deploy-llm.sh`'s own preflight (PLAN.md §0.5: reused
almost verbatim).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

VALID_CONFIG = """# scripts/ps-defaults.conf — evaluator-tunable Azure customer-tenant deployment
# defaults (issue #111). No secrets. See docs/architecture/customer-azure-deployment.md.

LLM_REGION_CANDIDATES=(swedencentral francecentral westeurope germanywestcentral)
LLM_CHAT_MODEL_NAME="gpt-5.4-mini"
LLM_CHAT_MODEL_SKU="DataZoneStandard"
LLM_CHAT_MODEL_CAPACITY=200
LLM_EMBED_MODEL_NAME="text-embedding-3-large"
LLM_EMBED_MODEL_SKU="Standard"
LLM_EMBED_MODEL_CAPACITY=350
TLS_CONTACT_EMAIL="tls-contact@example.test"
"""


def _seed(fixture: DeployPsFixture) -> None:
    fixture.config_path.write_text(VALID_CONFIG, encoding="utf-8")
    fixture.seed_subscription()  # bundles Owner by default; tests below override as needed


def _role_assignment_calls(fixture: DeployPsFixture) -> list[str]:
    """Only the subscription-ROOT-scoped `role assignment list` calls this module's own
    preflight (`rbac_preflight`) makes -- so this stays accurate once S13 adds a second,
    cluster-scoped `role assignment list` call of its own (`grant_aks_rbac_access`, a distinct
    scope for a distinct concern) later in the same `main()` run. Checks the `--scope` token's
    exact value, not merely a `/subscriptions/` substring -- the cluster's own resource ID is
    itself nested under `/subscriptions/<id>/...`, so a plain substring check would still match
    both calls.
    """
    calls: list[str] = []
    for line in fixture.read_az_log():
        if not line.startswith("role assignment list"):
            continue
        tokens = line.split()
        scope_value = tokens[tokens.index("--scope") + 1]
        if scope_value.count("/") == 2:  # "/subscriptions/<id>" only, no nested resource path
            calls.append(line)
    return calls


def _provider_calls(fixture: DeployPsFixture) -> list[str]:
    return [
        line
        for line in fixture.read_az_log()
        if line.startswith(("provider register", "provider show"))
    ]


def test_missing_owner_and_contributor_fails_before_provider_registration(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_role_assignments()  # no roles at all

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert "Owner" in run.stderr
    assert "Contributor" in run.stderr
    assert "az role assignment create" in run.stderr
    assert _provider_calls(deploy_ps_fixture) == []


def test_owner_role_present_passes(deploy_ps_fixture: DeployPsFixture) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_role_assignments("Owner")

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert len(_role_assignment_calls(deploy_ps_fixture)) == 1


def test_contributor_role_present_passes(deploy_ps_fixture: DeployPsFixture) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_role_assignments("Contributor")

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert len(_role_assignment_calls(deploy_ps_fixture)) == 1


def test_reader_role_alone_fails(deploy_ps_fixture: DeployPsFixture) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_role_assignments("Reader")

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert "Owner" in run.stderr
    assert "Contributor" in run.stderr
