"""Script-level proof that `scripts/deploy-ps.sh` makes no Key Vault or kubectl calls for
Authentik's or the signing-Postgres's credentials, and that `--rotate-authentik-secrets` no
longer exists (issue #159, AC-BI-006's script-side half / AC-BI-009).

Generation of these two secrets moved entirely into the Helm chart itself (a `lookup`+
`randAlphaNum` idiom in charts/policy-system/templates/authentik-credentials-secret.yaml/
signing-postgres-secret.yaml), created/updated as ordinary chart-rendered resources by
`helm upgrade --install` (this script's own `ensure_release`) -- this script no longer reads,
writes, or syncs either secret itself. The chart-rendering half of this proof (the Secrets
actually render correctly) lives in charts/policy-system/tests/*.yaml (helm-unittest), since this
Python harness's fake `helm` never actually renders the real chart (see conftest.py's own
FAKE_HELM_SCRIPT docstring) -- this module's job is only to prove the *script* makes no such
call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

# Mirrors DeployPsFixture.seed_subscription's default id_, same convention as every other
# deploy_ps test module's own constant.
SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)


def test_fresh_deploy_makes_no_keyvault_secret_set_calls_for_authentik_or_signing_postgres(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    secret_set_lines = [
        line for line in deploy_ps_fixture.read_az_log() if line.startswith("keyvault secret set")
    ]
    # Only the three LLM credentials are ever written to Key Vault now -- never
    # AUTHENTIK-SECRET-KEY/AUTHENTIK-POSTGRES-PASSWORD/PS-SERVICE-SIGNING-POSTGRES-PASSWORD,
    # which no longer exist as Key Vault secret names at all.
    assert secret_set_lines, "sanity: the LLM secrets themselves must still be written"
    assert not any("AUTHENTIK" in line for line in secret_set_lines)
    assert not any("SIGNING" in line for line in secret_set_lines)


def test_fresh_deploy_never_applies_the_authentik_or_signing_postgres_secret_itself(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """The chart -- not this script -- now owns both Secrets (see this module's own docstring);
    `deploy-ps.sh` never issues a `kubectl create secret .../apply` for either of them.
    """
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert (
        deploy_ps_fixture.read_kubectl_applied("Secret", "policy-system-authentik-credentials")
        is None
    )
    assert (
        deploy_ps_fixture.read_kubectl_applied(
            "Secret", "policy-system-signing-postgres-credentials"
        )
        is None
    )


def test_rotate_authentik_secrets_flag_no_longer_exists(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-009: the flag is removed outright (PLAN.md §0.6), not redefined -- an unknown-flag
    usage error, same as any other unrecognized argument (parse_args's own default case arm).
    """
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--rotate-authentik-secrets", expect=2)

    assert "unknown flag" in run.stderr
    assert "--rotate-authentik-secrets" in run.stderr
