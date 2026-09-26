"""Authentik's own secrets: generate-once + Key-Vault-sourced K8s Secret sync (issue #129,
IMPL_SLICE_7.md; mirrors `test_llm_secret_sync.py`'s own shape).

`ensure_authentik_secrets` mirrors `ensure_llm_secret`'s exact read-from-Key-Vault ->
write-if-changed -> `kubectl create secret --dry-run=client -o yaml | kubectl apply -f -` idiom,
with one difference `ensure_llm_secret` doesn't have: the LLM credentials already exist
externally (an Azure account's own key) and are only ever *read* from Azure and mirrored into
Key Vault, whereas Authentik's own Django `secret_key` and Postgres password have no external
source of truth -- they must be generated once, locally, on first run, then persisted and reused
on every subsequent run (never regenerated). `read_secret_value` returning an empty string is
this script's own signal that a value has never been generated before (same "empty means absent"
convention `write_secret_if_changed` already relies on elsewhere in this script).

Per IMPL_SLICE_3.md (S3's own load-bearing finding): the upstream `authentik` chart's
`existingSecret` mechanism is all-or-nothing once set -- the server/worker Deployments source
100% of their config via `envFrom: secretRef` against this one Secret, ignoring every
`authentik.authentik.*` non-secret value entirely. So this Secret
(`policy-system-authentik-credentials`) must carry ALL of `AUTHENTIK_POSTGRESQL__{HOST,PORT,USER,
PASSWORD,NAME}` plus `AUTHENTIK_SECRET_KEY` as keys, not just the two "secret" values -- the
non-secret connection values (host/port/user/name) are fixed literals matching S2's own hand-
rolled Postgres Service/database/user (`policy-system-authentik-postgres` / `5432` / `authentik`
/ `authentik`, confirmed against `charts/policy-system/values-prod.yaml`'s own documented
`authentik.authentik.postgresql.*` values), not read from Key Vault.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

AUTHENTIK_SECRET_NAME = "policy-system-authentik-credentials"
AUTHENTIK_POSTGRES_HOST = "policy-system-authentik-postgres"
AUTHENTIK_POSTGRES_PORT = "5432"
AUTHENTIK_POSTGRES_USER = "authentik"
AUTHENTIK_POSTGRES_DB = "authentik"

# Mirrors scripts/deploy-ps.sh's own AUTHENTIK-SECRET-KEY/AUTHENTIK-POSTGRES-PASSWORD Key Vault
# secret-name literals -- hardcoded here rather than parsed from the script, same precedent as
# every other deploy_ps test module's own constants.
KV_SECRET_KEY_NAME = "AUTHENTIK-SECRET-KEY"
KV_POSTGRES_PASSWORD_NAME = "AUTHENTIK-POSTGRES-PASSWORD"

# Mirrors DeployPsFixture.seed_subscription's default id_.
SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"


def _hash8(subscription_id: str) -> str:
    import hashlib

    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _vault_name(subscription_id: str = SUBSCRIPTION_ID) -> str:
    return f"kv-ps-llm-{_hash8(subscription_id)}"


def _seed(fixture: DeployPsFixture) -> None:
    fixture.fill_tls_contact_email()
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)


def _authentik_secret_apply_output_lines(fixture: DeployPsFixture) -> list[str]:
    """Filters `read_kubectl_apply_output_log()` down to only the Authentik credentials Secret's
    own lines -- other resources (`ClusterIssuer`, `Ingress`, the LLM Secret) are also applied on
    the same run, same filtering convention as `test_llm_secret_sync.py`'s own
    `_llm_secret_apply_output_lines`.
    """
    return [
        line
        for line in fixture.read_kubectl_apply_output_log()
        if line.startswith(f"secret/{AUTHENTIK_SECRET_NAME} ")
    ]


def test_writes_authentik_secret_with_every_required_key_on_a_fresh_run(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed(deploy_ps_fixture)

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Secret", AUTHENTIK_SECRET_NAME)
    assert manifest is not None, "kubectl apply -f - was never called for the Authentik Secret"

    assert f'AUTHENTIK_POSTGRESQL__HOST: "{AUTHENTIK_POSTGRES_HOST}"' in manifest
    assert f'AUTHENTIK_POSTGRESQL__PORT: "{AUTHENTIK_POSTGRES_PORT}"' in manifest
    assert f'AUTHENTIK_POSTGRESQL__USER: "{AUTHENTIK_POSTGRES_USER}"' in manifest
    assert f'AUTHENTIK_POSTGRESQL__NAME: "{AUTHENTIK_POSTGRES_DB}"' in manifest
    assert "AUTHENTIK_POSTGRESQL__PASSWORD: " in manifest
    assert "AUTHENTIK_SECRET_KEY: " in manifest

    # The generated values themselves are non-empty and were actually persisted to Key Vault
    # (never only held in-process) -- read back via the same vault a real operator's Key Vault
    # blade would show.
    vault = _vault_name()
    secret_key = deploy_ps_fixture.read_secret(vault, KV_SECRET_KEY_NAME)
    pg_password = deploy_ps_fixture.read_secret(vault, KV_POSTGRES_PASSWORD_NAME)
    assert secret_key, "AUTHENTIK-SECRET-KEY was never written to Key Vault"
    assert pg_password, "AUTHENTIK-POSTGRES-PASSWORD was never written to Key Vault"
    assert len(secret_key) >= 40, "generated Django secret_key looks too short to be secure"
    assert len(pg_password) >= 20, "generated Postgres password looks too short to be secure"
    assert f'AUTHENTIK_SECRET_KEY: "{secret_key}"' in manifest
    assert f'AUTHENTIK_POSTGRESQL__PASSWORD: "{pg_password}"' in manifest


def test_rerun_reuses_the_same_generated_values_and_reports_unchanged(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """The generate-once contract: a value with no external source of truth must never be
    silently regenerated on a rerun -- unlike the LLM key (re-fetched from the live Azure account
    every run), Authentik's secret_key/Postgres password are read back from Key Vault and reused
    verbatim once they exist.
    """
    _seed(deploy_ps_fixture)
    vault = _vault_name()

    deploy_ps_fixture.run_deploy("--yes", expect=0)
    first_lines = _authentik_secret_apply_output_lines(deploy_ps_fixture)
    assert first_lines == [f"secret/{AUTHENTIK_SECRET_NAME} created"]
    first_secret_key = deploy_ps_fixture.read_secret(vault, KV_SECRET_KEY_NAME)
    first_pg_password = deploy_ps_fixture.read_secret(vault, KV_POSTGRES_PASSWORD_NAME)

    deploy_ps_fixture.run_deploy("--yes", expect=0)
    second_lines = _authentik_secret_apply_output_lines(deploy_ps_fixture)

    assert second_lines == [
        f"secret/{AUTHENTIK_SECRET_NAME} created",
        f"secret/{AUTHENTIK_SECRET_NAME} unchanged",
    ]
    assert deploy_ps_fixture.read_secret(vault, KV_SECRET_KEY_NAME) == first_secret_key
    assert deploy_ps_fixture.read_secret(vault, KV_POSTGRES_PASSWORD_NAME) == first_pg_password

    # No second `keyvault secret set` for either value on the rerun (write-if-changed, same
    # idempotency contract as write_secret_if_changed's every other caller).
    az_log = deploy_ps_fixture.read_az_log()
    secret_set_lines = [line for line in az_log if line.startswith("keyvault secret set")]
    assert sum(1 for line in secret_set_lines if KV_SECRET_KEY_NAME in line) == 1
    assert sum(1 for line in secret_set_lines if KV_POSTGRES_PASSWORD_NAME in line) == 1


def test_generated_secret_values_never_appear_in_stdout_or_stderr(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """AC-BI-013's "never logged" discipline, extended to Authentik's own secrets -- matches
    `read_secret_value`/`write_secret_if_changed`'s own existing never-print convention for the
    LLM key.
    """
    _seed(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=0)

    vault = _vault_name()
    secret_key = deploy_ps_fixture.read_secret(vault, KV_SECRET_KEY_NAME)
    pg_password = deploy_ps_fixture.read_secret(vault, KV_POSTGRES_PASSWORD_NAME)
    assert secret_key and pg_password

    assert secret_key not in run.stdout
    assert secret_key not in run.stderr
    assert pg_password not in run.stdout
    assert pg_password not in run.stderr


def test_pre_existing_vault_values_are_reused_verbatim_rather_than_regenerated(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """A vault that already holds these two values (e.g. seeded out-of-band, or surviving a
    Key-Vault-only restore) must never be overwritten with a freshly-generated value -- same
    read-before-generate discipline `ensure_authentik_secrets` applies on every run, not just a
    literal "first run".
    """
    _seed(deploy_ps_fixture)
    vault = _vault_name()
    deploy_ps_fixture.seed_existing_secret(
        vault, KV_SECRET_KEY_NAME, "pre-existing-secret-key-value"
    )
    deploy_ps_fixture.seed_existing_secret(
        vault, KV_POSTGRES_PASSWORD_NAME, "pre-existing-pg-password"
    )

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    manifest = deploy_ps_fixture.read_kubectl_applied("Secret", AUTHENTIK_SECRET_NAME)
    assert manifest is not None
    assert 'AUTHENTIK_SECRET_KEY: "pre-existing-secret-key-value"' in manifest
    assert 'AUTHENTIK_POSTGRESQL__PASSWORD: "pre-existing-pg-password"' in manifest
