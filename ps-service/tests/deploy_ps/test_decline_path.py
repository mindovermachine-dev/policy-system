"""Decline path: answering `N` at the confirmation prompt (contributes AC-BI-019; PLAN.md §5/S5).

On `N`, `deploy-ps.sh` prints the config file path and exits 0 -- nothing past the confirmation
prompt runs (no RBAC preflight, no provider registration, no resource creation -- none of which
exist yet at S5, but the same call-log assertion `deploy-llm.sh`'s own decline-path tests use
still proves it here: no `az` call beyond the one needed to compute the table).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
CONFIG_DISPLAY_PATH = "scripts/ps-defaults.conf"

CONFIG_WITH_TLS_EMAIL = """# scripts/ps-defaults.conf — evaluator-tunable Azure customer-tenant
# deployment defaults (issue #111). No secrets. See docs/architecture/customer-azure-deployment.md.

LLM_REGION_CANDIDATES=(swedencentral francecentral westeurope germanywestcentral)
LLM_CHAT_MODEL_NAME="gpt-5.4-mini"
LLM_CHAT_MODEL_SKU="DataZoneStandard"
LLM_CHAT_MODEL_CAPACITY=200
LLM_EMBED_MODEL_NAME="text-embedding-3-large"
LLM_EMBED_MODEL_SKU="Standard"
LLM_EMBED_MODEL_CAPACITY=350
TLS_CONTACT_EMAIL="tls-contact@example.test"
"""


def _azure_state_listing(fixture: DeployPsFixture) -> list[str]:
    return sorted(
        str(path.relative_to(fixture.azure_state)) for path in fixture.azure_state.rglob("*")
    )


def _seed(fixture: DeployPsFixture) -> None:
    fixture.config_path.write_text(CONFIG_WITH_TLS_EMAIL, encoding="utf-8")
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)


def test_answering_n_exits_zero_and_prints_config_file_path(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    before = _azure_state_listing(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(stdin="N\n", expect=0)

    assert CONFIG_DISPLAY_PATH in run.stdout
    # Declining changes nothing in the subscription -- the pre-seeded identity files are the
    # only thing on disk, still.
    assert _azure_state_listing(deploy_ps_fixture) == before


def test_answering_n_makes_no_az_calls_beyond_account_show(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy(stdin="N\n", expect=0)

    assert deploy_ps_fixture.read_az_log() == ["account show --query id -o tsv"]
