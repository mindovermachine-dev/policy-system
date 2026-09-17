"""Decline path: answering `N` at the confirmation prompt (AC-BI-003; PLAN.md §5/S3).

On `N`, `deploy-llm.sh` prints the config file path and exits 0 -- nothing past step 3 of
PLAN.md §0.1's resolved sequence runs (no RBAC preflight, no region selection, no writes).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
CONFIG_DISPLAY_PATH = "scripts/llm-defaults.conf"


def _azure_state_listing(fixture: DeployLlmFixture) -> list[str]:
    return sorted(
        str(path.relative_to(fixture.azure_state)) for path in fixture.azure_state.rglob("*")
    )


def test_answering_n_exits_zero_and_prints_config_file_path(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    before = _azure_state_listing(deploy_llm_fixture)

    run = deploy_llm_fixture.run_deploy(stdin="N\n", expect=0)

    assert CONFIG_DISPLAY_PATH in run.stdout
    # Declining changes nothing in the subscription (AC-BI-003's "exits without changing
    # anything") -- the pre-seeded identity files are the only thing on disk, still.
    assert _azure_state_listing(deploy_llm_fixture) == before


def test_answering_n_makes_no_az_calls_beyond_account_show(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)

    deploy_llm_fixture.run_deploy(stdin="N\n", expect=0)

    assert deploy_llm_fixture.read_az_log() == ["account show --query id -o tsv"]
