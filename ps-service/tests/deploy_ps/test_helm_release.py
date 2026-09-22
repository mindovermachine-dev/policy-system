"""Helm release: scopes/audience wiring + narrow no-op comparison (AC-BI-001 completion,
AC-BI-002 script-half, AC-BI-018; PLAN.md §5/S15).

CHANGES.md's Appendix A correction applies here, not PLAN.md's original text: the no-op
comparison covers exactly **5** leaf fields -- `llm.existingSecret`,
`psService.auth.{issuer,audience,cliClientId,scopes}` -- not the "4 fields" PLAN.md's own
`release_values_json`/`ensure_release` prose and this module's own test name originally
miscounted (FLAWS.md's MAJOR finding). Ground truth independently re-verified against
`spikes/deploy-ps-azure/deploy-ps.sh:1015-1065`'s own `release_values_json`/`ensure_release`,
the trusted empirical reference.

`psService.auth.audience` and `psService.auth.scopes` are deliberately DIFFERENT formats for
DIFFERENT purposes (docs/artifacts/idp-configuration-contract.md):
- `audience` is the **bare** API app ID GUID -- Entra normalizes a device-flow token's `aud`
  claim to this bare form for the self-referencing `api://<own-client-id>` Application ID URI
  pattern this script uses (idp-configuration-contract.md Step 10 / Common pitfalls). Configuring
  the `api://...` URI form here is the exact AC-BI-002 regression the spike's exit-criterion-5
  debugging found: login succeeds, every API call still 401s.
- `scopes` is the **URI** form, `api://<api-app-id>/access_as_user` -- the OAuth scope PS-Cli's
  device-flow login requests, never used for token validation itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors scripts/deploy-ps.sh's own API_APP_NAME/CLI_APP_NAME/HELM_RELEASE_NAME/LLM_SECRET_NAME
# literals, and DeployPsFixture.seed_subscription's own default tenant_id -- hardcoded here
# rather than imported, same precedent as every other deploy_ps test module (conftest.py isn't a
# runtime-importable module from a test file collected this way).
API_APP_NAME = "Policy System API"
CLI_APP_NAME = "Policy System CLI"
HELM_RELEASE_NAME = "policy-system"
LLM_SECRET_NAME = "policy-system-llm-credentials"
DEFAULT_TENANT_ID = "33333333-4444-5555-6666-777777777777"


def fake_app_id(display_name: str) -> str:
    """Independently reproduce conftest.py's fake `az ad app create`'s deterministic appId
    ("appid-<slugified display name>") -- same helper as every other deploy_ps test module's own
    `fake_app_id`.
    """
    return f"appid-{display_name.lower().replace(' ', '-')}"


API_APP_ID = fake_app_id(API_APP_NAME)
CLI_APP_ID = fake_app_id(CLI_APP_NAME)
EXPECTED_ISSUER = f"https://login.microsoftonline.com/{DEFAULT_TENANT_ID}/v2.0"
EXPECTED_AUDIENCE = API_APP_ID
EXPECTED_SCOPES = f"api://{API_APP_ID}/access_as_user"


def _llm(values: dict[str, object]) -> dict[str, str]:
    """Narrows `values["llm"]` from `read_helm_release_values`'s generic `dict[str, object]`
    (the release payload's shape is untyped JSON) down to `dict[str, str]` for this module's one
    leaf string field -- basedpyright strict mode has no way to know the nested shape itself, so
    an explicit cast is unavoidable to index a second level in.
    """
    return cast("dict[str, str]", values["llm"])


def _auth(values: dict[str, object]) -> dict[str, str]:
    """Narrows `values["psService"]["auth"]` the same way `_llm` narrows `values["llm"]` -- see
    its docstring for why the cast is unavoidable.
    """
    ps_service = cast("dict[str, object]", values["psService"])
    return cast("dict[str, str]", ps_service["auth"])


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()


def _upgrade_calls(fixture: DeployPsFixture) -> list[str]:
    """Filters `read_helm_log()` down to only the "policy-system" release's own `upgrade
    --install` calls -- S17 (a later slice) also `helm upgrade --install`s a separate
    "cert-manager" release on a fresh run, which would otherwise be miscounted here as a change
    to this release.
    """
    return [
        line
        for line in fixture.read_helm_log()
        if line.startswith(f"upgrade --install {HELM_RELEASE_NAME} ")
    ]


def test_fresh_release_sets_existing_secret_issuer_audience_cli_client_id_and_scopes(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    values = deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert values is not None, "helm upgrade --install was never called"
    assert _llm(values)["existingSecret"] == LLM_SECRET_NAME
    auth = _auth(values)
    assert auth["issuer"] == EXPECTED_ISSUER
    assert auth["audience"] == EXPECTED_AUDIENCE
    assert auth["cliClientId"] == CLI_APP_ID
    assert auth["scopes"] == EXPECTED_SCOPES


def test_audience_is_the_bare_api_app_id_not_the_api_uri_form(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """The exact AC-BI-002 regression this guards: configuring `psService.auth.audience` as the
    `api://<id>` URI form (instead of the bare GUID) makes login succeed while every subsequent
    API call still 401s (idp-configuration-contract.md's own documented Entra `aud`-claim
    quirk). Confirmed red-before-green: temporarily swapping `ensure_release`'s `--set
    psService.auth.audience="$audience"` argument for `"api://$audience"` in
    scripts/deploy-ps.sh makes this assertion fail (see IMPL_SLICE_15.md's evidence block).
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    values = deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert values is not None
    audience = _auth(values)["audience"]
    assert audience == API_APP_ID
    assert audience != f"api://{API_APP_ID}"


def test_scopes_is_the_api_uri_form_access_as_user_string(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """`scopes` uses the OPPOSITE convention from `audience` -- proves the two are not
    accidentally set to the same value/format (a mistake that would pass a test checking either
    field in isolation).
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    values = deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert values is not None
    auth = _auth(values)
    assert auth["scopes"] == f"api://{API_APP_ID}/access_as_user"
    assert auth["scopes"] != auth["audience"]


def test_rerun_with_unchanged_inputs_makes_no_helm_upgrade_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-018's literal claim: a rerun against unchanged inputs makes zero `helm upgrade`
    calls (not merely "reports success" -- the call itself must not happen).
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)
    first_calls = _upgrade_calls(deploy_ps_fixture)
    assert len(first_calls) == 1

    deploy_ps_fixture.run_deploy("--yes", expect=0)
    second_calls = _upgrade_calls(deploy_ps_fixture)

    assert second_calls == first_calls


def test_rerun_only_compares_the_five_script_set_fields_not_falkordb_or_llm_provider_from_values_prod(  # noqa: E501 - CHANGES.md Appendix A's exact corrected test name, not abbreviated
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """CHANGES.md Appendix A's corrected test (5 fields, not PLAN.md's originally miscounted 4):
    seeds `helm get values` with the 5 fields a fresh run would itself produce, PLUS extra fields
    that only ever come from `-f values-prod.yaml` and this script never sets itself
    (`falkordb.persistence.durableStorageClass`, `llm.provider`, `psService.service.type`) --
    proving the no-op comparison still reports "unchanged" (no new `helm upgrade` call) despite
    those extra fields being present. A whole-object comparison (the bug this guards, per the
    spike's own "Bugs found and fixed" section) would never match here, since
    `release_values_json` never produces `falkordb`/`llm.provider`/`psService.service` keys at
    all -- permanently defeating idempotency.
    """
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_helm_release(
        {
            "llm": {"existingSecret": LLM_SECRET_NAME, "provider": "azure"},
            "psService": {
                "auth": {
                    "issuer": EXPECTED_ISSUER,
                    "audience": EXPECTED_AUDIENCE,
                    "cliClientId": CLI_APP_ID,
                    "scopes": EXPECTED_SCOPES,
                },
                "service": {"type": "ClusterIP"},
            },
            "falkordb": {
                "persistence": {"durableStorageClass": {"enabled": True}},
                "browser": {"enabled": False},
            },
        },
        release=HELM_RELEASE_NAME,
    )

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert _upgrade_calls(deploy_ps_fixture) == []


def test_changed_scopes_triggers_a_new_helm_upgrade_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.seed_helm_release(
        {
            "llm": {"existingSecret": LLM_SECRET_NAME},
            "psService": {
                "auth": {
                    "issuer": EXPECTED_ISSUER,
                    "audience": EXPECTED_AUDIENCE,
                    "cliClientId": CLI_APP_ID,
                    "scopes": "api://some-stale-app-id/access_as_user",
                },
            },
        },
        release=HELM_RELEASE_NAME,
    )

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    calls = _upgrade_calls(deploy_ps_fixture)
    assert len(calls) == 1
    values = deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert values is not None
    assert _auth(values)["scopes"] == EXPECTED_SCOPES
