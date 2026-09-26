"""`--rotate-authentik-secrets` mode for `scripts/deploy-ps.sh` (AC-BI-009's rotation half,
issue #129 S9). Mirrors `test_rotate_key.py`'s own shape: branches immediately after flag
parsing, before any of the normal provisioning body runs, and fails clearly if run before a
first successful deploy.

Unlike `--rotate-key` (an Azure-only operation -- the LLM API key is regenerated on the live
AIServices account and only written to Key Vault; nothing in the cluster is touched),
`--rotate-authentik-secrets` regenerates BOTH of Authentik's own generate-once secrets (the
Django `secret_key` and the Postgres password, S7/IMPL_SLICE_7.md), re-syncs the cluster's
`policy-system-authentik-credentials` Secret immediately, and must therefore also:

  1. actually change the LIVE Postgres role's password (a plain postgres image only ever
     consumes `POSTGRES_PASSWORD` at first-init time -- once $PGDATA already holds an
     initialized cluster, as it always does after the first real deploy since S2's PVC persists
     it, the entrypoint script never re-reads `POSTGRES_PASSWORD` on a restart. Restarting the
     Postgres Deployment alone would therefore silently desync the new Secret value from the
     real, unchanged DB password -- locking Authentik out of its own DB the next time its own
     pods restart and reconnect. The only correct fix is an in-database `ALTER USER`, issued
     directly against the already-running Postgres pod via `kubectl exec`);
  2. restart the Authentik server/worker Deployments (rotating the Django `secret_key`
     invalidates every existing session, and those pods only read their envFrom-sourced config
     once, at process start -- `--rotate-key`'s own LLM-key rotation needs no such restart,
     since it never re-syncs the cluster Secret in the first place).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"

AUTHENTIK_SECRET_NAME = "policy-system-authentik-credentials"
AUTHENTIK_SERVER_DEPLOYMENT = "policy-system-authentik-server"
AUTHENTIK_WORKER_DEPLOYMENT = "policy-system-authentik-worker"
AUTHENTIK_POSTGRES_DEPLOYMENT = "policy-system-authentik-postgres"

KV_SECRET_KEY_NAME = "AUTHENTIK-SECRET-KEY"
KV_POSTGRES_PASSWORD_NAME = "AUTHENTIK-POSTGRES-PASSWORD"

INITIAL_SECRET_KEY = "initial-fake-django-secret-key-0123456789"
INITIAL_PG_PASSWORD = "initial-fake-pg-password"


def _hash8(subscription_id: str) -> str:
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _names(subscription_id: str = SUBSCRIPTION_ID) -> tuple[str, str]:
    hash8 = _hash8(subscription_id)
    return f"kv-ps-llm-{hash8}", f"aks-policy-system-{hash8}"


def _seed_provisioned_authentik(fixture: DeployPsFixture) -> tuple[str, str]:
    """Seeds a subscription that already had a full, successful `deploy-ps.sh` run: the Key
    Vault and AKS cluster exist, and Authentik's own two generate-once secrets are already
    present in Key Vault -- exactly the state `rotate_authentik_secrets_main` requires.
    """
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    vault, cluster = _names()
    fixture.seed_existing_keyvault(vault)
    fixture.seed_aks_cluster(cluster, subscription_id=SUBSCRIPTION_ID)
    fixture.seed_existing_secret(vault, KV_SECRET_KEY_NAME, INITIAL_SECRET_KEY)
    fixture.seed_existing_secret(vault, KV_POSTGRES_PASSWORD_NAME, INITIAL_PG_PASSWORD)
    return vault, cluster


def test_rotate_authentik_secrets_regenerates_both_values_and_never_prints_them(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    vault, _cluster = _seed_provisioned_authentik(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--rotate-authentik-secrets", expect=0)

    new_secret_key = deploy_ps_fixture.read_secret(vault, KV_SECRET_KEY_NAME)
    new_pg_password = deploy_ps_fixture.read_secret(vault, KV_POSTGRES_PASSWORD_NAME)
    assert new_secret_key and new_secret_key != INITIAL_SECRET_KEY
    assert new_pg_password and new_pg_password != INITIAL_PG_PASSWORD

    assert INITIAL_SECRET_KEY not in run.output
    assert INITIAL_PG_PASSWORD not in run.output
    assert new_secret_key not in run.output
    assert new_pg_password not in run.output


def test_rotate_authentik_secrets_reapplies_the_cluster_secret_with_the_new_values(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    vault, _cluster = _seed_provisioned_authentik(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--rotate-authentik-secrets", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Secret", AUTHENTIK_SECRET_NAME)
    assert manifest is not None, "kubectl apply -f - was never called for the Authentik Secret"

    new_secret_key = deploy_ps_fixture.read_secret(vault, KV_SECRET_KEY_NAME)
    new_pg_password = deploy_ps_fixture.read_secret(vault, KV_POSTGRES_PASSWORD_NAME)
    assert f'AUTHENTIK_SECRET_KEY: "{new_secret_key}"' in manifest
    assert f'AUTHENTIK_POSTGRESQL__PASSWORD: "{new_pg_password}"' in manifest


def test_rotate_authentik_secrets_alters_the_live_postgres_role_password(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Confirms the actual DB-level password change happens (an `ALTER USER` issued via
    `kubectl exec` against the running Postgres pod), not merely a Deployment restart -- a plain
    postgres image ignores `POSTGRES_PASSWORD` once its data directory is already initialized,
    so a restart alone would silently lock Authentik out of its own DB.
    """
    _seed_provisioned_authentik(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--rotate-authentik-secrets", expect=0)

    log = deploy_ps_fixture.read_kubectl_log()
    assert any(
        line.startswith(f"exec deployment/{AUTHENTIK_POSTGRES_DEPLOYMENT}") and "ALTER USER" in line
        for line in log
    ), "no in-database ALTER USER was issued against the live Postgres pod"

    # Postgres itself is never restarted -- a restart would not change the live role's password
    # anyway (see module docstring), so doing one would be pure noise/downtime.
    assert f"rollout restart deployment/{AUTHENTIK_POSTGRES_DEPLOYMENT}" not in log


def test_rotate_authentik_secrets_restarts_authentik_server_and_worker(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """Rotating the Django `secret_key` invalidates every existing session, and the server/
    worker pods only read their envFrom-sourced config once, at process start -- this rotation
    (unlike `--rotate-key`) re-syncs the cluster Secret immediately, so it must also force both
    Deployments to restart so the new secret_key takes effect right away.
    """
    _seed_provisioned_authentik(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--rotate-authentik-secrets", expect=0)

    log = deploy_ps_fixture.read_kubectl_log()
    assert f"rollout restart deployment/{AUTHENTIK_SERVER_DEPLOYMENT}" in log
    assert f"rollout restart deployment/{AUTHENTIK_WORKER_DEPLOYMENT}" in log


def test_rotate_authentik_secrets_skips_config_validation_confirmation_and_preflight(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed_provisioned_authentik(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--rotate-authentik-secrets", expect=0)

    az_log = deploy_ps_fixture.read_az_log()
    assert not any(line.startswith("role assignment list") for line in az_log)
    assert not any(line.startswith("cognitiveservices model list") for line in az_log)
    assert not any(line.startswith("aks create") for line in az_log)


def test_rotate_authentik_secrets_run_before_any_successful_deploy_fails_clearly(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """No Key Vault (nor, by construction, its two Authentik secrets) exists yet for a
    subscription that has never had a successful `deploy-ps.sh` run -- must fail with an
    actionable message, not crash on an unset variable or an unhandled `az`/`kubectl` error.
    """
    deploy_ps_fixture.seed_subscription(id_=SUBSCRIPTION_ID)

    run = deploy_ps_fixture.run_deploy("--rotate-authentik-secrets", expect=1)

    assert "deploy-ps.sh" in run.stderr
    assert "--rotate-authentik-secrets" in run.stderr


def test_rotate_authentik_secrets_run_before_first_rotation_generation_fails_clearly(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """A Key Vault can exist (e.g. from LLM provisioning) without Authentik's own secrets ever
    having been generated (the vault-exists check alone is not sufficient) -- must still fail
    clearly rather than "rotate" an empty value into place.
    """
    deploy_ps_fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    vault, cluster = _names()
    deploy_ps_fixture.seed_existing_keyvault(vault)
    deploy_ps_fixture.seed_aks_cluster(cluster, subscription_id=SUBSCRIPTION_ID)

    run = deploy_ps_fixture.run_deploy("--rotate-authentik-secrets", expect=1)

    assert "deploy-ps.sh" in run.stderr
    assert "--rotate-authentik-secrets" in run.stderr
