"""Capacity-range validation against the selected region's live-reported range (AC-BI-009/010),
plus the spike's null-capacity bugfix (PLAN.md §5/S8).

`validate_capacity_range` runs once, against `select_region`'s already-chosen region only (no
fallback-region reporting -- unlike `scripts/deploy-llm.sh`'s post-#110 `fail_region_not_viable`,
`deploy-ps.sh` never auto-picks a *different* region once one is already selected; a capacity
failure here is a hard stop).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture


def test_capacity_minimum_null_coalesces_to_zero_not_a_bash_crash(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Bugfix 1/6 (spikes/deploy-ps-azure/README.md "Bugs found and fixed": "capacity.minimum:
    null"). Azure reports `capacity.minimum: null` for SKUs with no enforced floor
    (GlobalStandard/DataZoneStandard, which `scripts/ps-defaults.conf` actually configures) --
    `model_capacity_range`'s bash arithmetic must coalesce this to `0` via `// 0` in the `jq`
    query, not crash on the literal string "null". The conftest baseline already defaults to a
    null minimum (see `seed_model_availability`'s own docstring), so this test is a stronger,
    explicit restatement of that default, isolated so a regression here fails on its own even if
    another test's baseline silently changes.
    """
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    deploy_ps_fixture.seed_model_availability(
        "swedencentral",
        chat_ga=True,
        embed_ga=True,
        chat_capacity_minimum=None,
        chat_capacity_maximum=3000,
    )

    deploy_ps_fixture.run_deploy("--yes", expect=0)


def test_chat_capacity_above_reported_maximum_fails_showing_actual_range(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    # Config's LLM_CHAT_MODEL_CAPACITY is 200 -- a reported maximum of 100 is below it.
    deploy_ps_fixture.seed_model_availability(
        "swedencentral", chat_ga=True, embed_ga=True, chat_capacity_maximum=100
    )

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert "LLM_CHAT_MODEL_CAPACITY" in run.stderr
    assert "0-100" in run.stderr


def test_embed_capacity_below_reported_minimum_fails_showing_actual_range(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    # Config's LLM_EMBED_MODEL_CAPACITY is 350 -- a reported minimum of 400 is above it.
    deploy_ps_fixture.seed_model_availability(
        "swedencentral",
        chat_ga=True,
        embed_ga=True,
        embed_capacity_minimum=400,
        embed_capacity_maximum=700,
    )

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert "LLM_EMBED_MODEL_CAPACITY" in run.stderr
    assert "400-700" in run.stderr


def test_capacity_within_range_passes(deploy_ps_fixture: DeployPsFixture) -> None:
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()  # conftest baseline's ranges comfortably fit 200/350

    deploy_ps_fixture.run_deploy("--yes", expect=0)
