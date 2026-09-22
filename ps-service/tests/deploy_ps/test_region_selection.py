"""Region selection: `select_region` probes `LLM_REGION_CANDIDATES` in configured order,
stopping at the first candidate where both models are Generally Available (AC-BI-009), and
hard-stops naming every candidate tried if none qualify (AC-BI-010).

Unlike `scripts/deploy-llm.sh` (issue #105, superseded by #110 into a single configured
`LLM_REGION` with no auto-switching), `deploy-ps.sh` has no single target region -- selection
*is* the mechanism (PLAN.md §5/S8), a flat loop with an early exit
(spikes/deploy-ps-azure/deploy-ps.sh's own `select_region`), not deploy-llm.sh's
fallback-viability-reporting shape. See PLAN.md §5/S8 and
.orchestrator/tracker/issue-111-deploy-ps-azure-script/IMPL_SLICE_7.md's "Next extension point"
for the exact `main()` splice point this slice fills.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

ALL_CANDIDATES = ("swedencentral", "francecentral", "westeurope", "germanywestcentral")


def _model_list_calls(fixture: DeployPsFixture) -> list[str]:
    return [
        line for line in fixture.read_az_log() if line.startswith("cognitiveservices model list")
    ]


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()  # GA everywhere by default (conftest baseline)


def test_region_selection_picks_first_candidate_with_both_models_generally_available(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    # swedencentral (first candidate) already satisfies both models -- no other candidate is
    # ever probed.
    assert _model_list_calls(deploy_ps_fixture) == [
        "cognitiveservices model list --location swedencentral",
    ]
    assert "Selected region: swedencentral" in run.stderr


def test_region_selection_skips_regions_without_availability(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    # swedencentral is not viable (embed not GA there) -- francecentral (still GA per baseline)
    # must be the one actually selected.
    deploy_ps_fixture.seed_model_availability("swedencentral", chat_ga=True, embed_ga=False)

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _model_list_calls(deploy_ps_fixture) == [
        "cognitiveservices model list --location swedencentral",
        "cognitiveservices model list --location francecentral",
    ]
    assert "Selected region: francecentral" in run.stderr


def test_region_selection_fails_explicitly_naming_every_candidate_tried(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    for region in ALL_CANDIDATES:
        deploy_ps_fixture.seed_model_availability(region, chat_ga=False, embed_ga=False)

    run = deploy_ps_fixture.run_deploy("--yes", expect=1)

    assert _model_list_calls(deploy_ps_fixture) == [
        f"cognitiveservices model list --location {region}" for region in ALL_CANDIDATES
    ]
    assert "No candidate region has both" in run.stderr
    assert "swedencentral, francecentral, westeurope, germanywestcentral" in run.stderr
