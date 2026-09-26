"""Helm release: Authentik-issuer wiring + narrow no-op comparison (AC-BI-001 completion,
AC-BI-005, AC-BI-018; PLAN.md §5/S15, reworked for issue #129's S8).

Issue #129 replaces the Entra-app-registration-derived `issuer`/`audience`/`cliClientId`/`scopes`
computation with fixed constants pointed at the bundled Authentik instance instead (zero Entra
app registrations, AC-BI-001) -- CHANGES.md row F1's path-based issuer shape
(`https://<hostname>/auth/application/o/ps-cli/`, sharing PS Service's own already-resolved
hostname, not a second Authentik-only hostname) and row F3 (S7/S8/S6 land as one combined unit,
built in that internal order).

The no-op comparison still covers exactly the same **5** leaf fields as before -- `llm.
existingSecret`, `psService.auth.{issuer,audience,cliClientId,scopes}` -- ground truth unchanged
from `ensure_release`'s own shape (`release_values_json`), only the *values* fed into it changed.

`psService.auth.audience`/`cliClientId` are now both the fixed literal `ps-cli` (S4's blueprint:
one OAuth2 Provider, one Application, both named `ps-cli` -- PLAN.md §0.5, no Entra-style
API-app-vs-CLI-app split). `scopes` is `openid profile email offline_access` -- S4's blueprint's
own actual `property_mappings` (IMPL_SLICE_0B's Bug 3 finding: without an `offline_access` scope
mapping bound to the Provider, Authentik never issues a `refresh_token`, breaking every ps-cli
command after the first `auth login`), never Entra's `api://<id>/access_as_user` URI form.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors scripts/deploy-ps.sh's own HELM_RELEASE_NAME/LLM_SECRET_NAME/AUTHENTIK_APP_SLUG/
# AUTHENTIK_SCOPES literals -- hardcoded here rather than imported, same precedent as every other
# deploy_ps test module (conftest.py isn't a runtime-importable module from a test file collected
# this way).
HELM_RELEASE_NAME = "policy-system"
LLM_SECRET_NAME = "policy-system-llm-credentials"
AUTHENTIK_APP_SLUG = "ps-cli"
AUTHENTIK_SCOPES = "openid profile email offline_access"


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
    --install` calls -- a fresh run also `helm upgrade --install`s a separate "cert-manager"
    release, which would otherwise be miscounted here as a change to this release.
    """
    return [
        line
        for line in fixture.read_helm_log()
        if line.startswith(f"upgrade --install {HELM_RELEASE_NAME} ")
    ]


def test_fresh_release_sets_existing_secret_and_authentik_issuer_audience_cli_client_id_and_scopes(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    values = deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert values is not None, "helm upgrade --install was never called"
    assert _llm(values)["existingSecret"] == LLM_SECRET_NAME
    auth = _auth(values)
    # Path-based issuer (CHANGES.md F1): "https://<same-hostname-as-ps-service>/auth/
    # application/o/ps-cli/" -- never a second, Authentik-only hostname.
    assert auth["issuer"].startswith("https://")
    assert auth["issuer"].endswith(f"/auth/application/o/{AUTHENTIK_APP_SLUG}/")
    assert auth["audience"] == AUTHENTIK_APP_SLUG
    assert auth["cliClientId"] == AUTHENTIK_APP_SLUG
    assert auth["scopes"] == AUTHENTIK_SCOPES


def test_issuer_is_path_based_under_ps_services_own_hostname_never_a_second_hostname(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """CHANGES.md row F1: rejects the original dual-hostname/dual-DNS-label design (infeasible --
    one Azure Public IP has exactly one `dnsSettings.domainNameLabel`). The issuer's hostname
    must be the *exact same* hostname PS Service's own Ingress uses, never Entra's
    `login.microsoftonline.com`, and never a distinct `auth-*` label.
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    values = deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert values is not None
    issuer = _auth(values)["issuer"]
    assert "login.microsoftonline.com" not in issuer

    issuer_hostname = issuer.removeprefix("https://").split("/", 1)[0]
    ps_service_ingress = deploy_ps_fixture.read_kubectl_applied(
        "Ingress", f"{HELM_RELEASE_NAME}-ps-service"
    )
    assert ps_service_ingress is not None, "PS Service Ingress was never applied"
    assert f"host: {issuer_hostname}" in ps_service_ingress


def test_scopes_includes_offline_access_so_ps_cli_ever_receives_a_refresh_token(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """IMPL_SLICE_0B's Bug 3 (live-checkpoint finding): without `offline_access` in the
    advertised scopes, ps-cli's device-flow login never receives a `refresh_token` from
    Authentik, breaking every command after the very first `auth login`.
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    values = deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert values is not None
    scopes = _auth(values)["scopes"]
    assert "offline_access" in scopes.split()


def test_audience_and_cli_client_id_are_both_the_fixed_ps_cli_literal_not_an_entra_app_id(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """PLAN.md §0.5: one fixed OAuth2 Provider/Application (`ps-cli`), never an Entra-style
    API-app-vs-CLI-app split -- `audience` and `cliClientId` are the SAME literal, unlike the old
    Entra shape where they were two distinct app IDs.
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    values = deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert values is not None
    auth = _auth(values)
    assert auth["audience"] == "ps-cli"
    assert auth["cliClientId"] == "ps-cli"
    assert auth["audience"] == auth["cliClientId"]


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
    """Seeds `helm get values` with the 5 fields a fresh run would itself produce, PLUS extra
    fields that only ever come from `-f values-prod.yaml` and this script never sets itself
    (`falkordb.persistence.durableStorageClass`, `llm.provider`, `psService.service.type`) --
    proving the no-op comparison still reports "unchanged" (no new `helm upgrade` call) despite
    those extra fields being present. A whole-object comparison (the bug this guards, per the
    spike's own "Bugs found and fixed" section) would never match here, since
    `release_values_json` never produces `falkordb`/`llm.provider`/`psService.service` keys at
    all -- permanently defeating idempotency.

    The exact issuer value must be read back from a first real run rather than hardcoded, since
    it is now derived from the fixture's own seeded public-IP/DNS-label state (S16), not a static
    Entra tenant-id literal.
    """
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", expect=0)
    first_values = deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert first_values is not None
    first_auth = _auth(first_values)
    upgrade_calls_after_first_run = len(_upgrade_calls(deploy_ps_fixture))

    deploy_ps_fixture.seed_helm_release(
        {
            "llm": {"existingSecret": LLM_SECRET_NAME, "provider": "azure"},
            "psService": {
                "auth": dict(first_auth),
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

    # No NEW upgrade call beyond the one the first, real run already made -- the seeded release
    # (re-stating the exact same 5 fields plus values-prod.yaml-only extras) must compare as
    # unchanged.
    assert len(_upgrade_calls(deploy_ps_fixture)) == upgrade_calls_after_first_run


def test_changed_scopes_triggers_a_new_helm_upgrade_call(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)
    deploy_ps_fixture.run_deploy("--yes", expect=0)
    first_values = deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert first_values is not None
    first_auth = dict(_auth(first_values))
    upgrade_calls_after_first_run = len(_upgrade_calls(deploy_ps_fixture))
    stale_auth = dict(first_auth)
    stale_auth["scopes"] = "openid profile email"

    deploy_ps_fixture.seed_helm_release(
        {
            "llm": {"existingSecret": LLM_SECRET_NAME},
            "psService": {"auth": stale_auth},
        },
        release=HELM_RELEASE_NAME,
    )

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    calls = _upgrade_calls(deploy_ps_fixture)
    assert len(calls) == upgrade_calls_after_first_run + 1
    values = deploy_ps_fixture.read_helm_release_values(HELM_RELEASE_NAME)
    assert values is not None
    assert _auth(values)["scopes"] == AUTHENTIK_SCOPES
