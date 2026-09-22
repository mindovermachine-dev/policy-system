"""`--rotate-key` mode for `scripts/deploy-ps.sh`: active-slot detection + regenerate-the-
inactive-key, carried over from `scripts/deploy-llm.sh`'s own proven implementation (AC-BI-015
completion, AC-BI-017's mocked portion; PLAN.md §5/S18).

Branches immediately after flag parsing (`parse_args`, checked in `main()` BEFORE any of
S5-S18's provisioning body runs) -- skips config validation, the confirmation table, RBAC
preflight, and region/quota/AKS/Helm provisioning entirely, none of which matter for rotating an
already-provisioned account's key. Reuses `llm_account_name`/`llm_keyvault_name` from
`scripts/lib/deploy-llm-common.sh` -- the exact same names `main()`'s own S9 step would compute
for this subscription -- and `require_account_exists`/`require_keyvault_exists`, the same
preflight-guard shape `scripts/deploy-llm.sh`'s own `--rotate-key` uses (mirrors
`ps-service/tests/deploy_llm/test_rotate_key.py`'s own structure).
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
DEFAULT_KEY1 = "FAKE-KEY-1-INITIAL"
DEFAULT_KEY2 = "FAKE-KEY-2-INITIAL"


def _hash8(subscription_id: str) -> str:
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _names(subscription_id: str = SUBSCRIPTION_ID) -> tuple[str, str]:
    hash8 = _hash8(subscription_id)
    return f"policy-system-llm-{hash8}", f"kv-ps-llm-{hash8}"


def _seed_provisioned_account(
    fixture: DeployPsFixture, *, active_secret_value: str
) -> tuple[str, str]:
    """Seeds an existing account/keyvault with a stored AZURE-API-KEY secret matching
    <active_secret_value> -- whichever of key1/key2 that equals is the "active" slot rotation
    must leave untouched.
    """
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    account, vault = _names()
    fixture.seed_existing_account(account, key1=DEFAULT_KEY1, key2=DEFAULT_KEY2)
    fixture.seed_existing_keyvault(vault)
    fixture.seed_existing_secret(vault, "AZURE-API-KEY", active_secret_value)
    return account, vault


def _read_keys(fixture: DeployPsFixture, account: str) -> dict[str, str]:
    path = fixture.azure_state / "accounts" / f"{account}-keys.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_rotate_key_regenerates_the_inactive_slot_and_never_prints_key_values(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    account, vault = _seed_provisioned_account(deploy_ps_fixture, active_secret_value=DEFAULT_KEY1)

    run = deploy_ps_fixture.run_deploy("--rotate-key", expect=0)

    keys = _read_keys(deploy_ps_fixture, account)
    assert keys["key1"] == DEFAULT_KEY1
    assert keys["key2"] != DEFAULT_KEY2
    assert deploy_ps_fixture.read_secret(vault, "AZURE-API-KEY") == keys["key2"]
    assert DEFAULT_KEY1 not in run.output
    assert DEFAULT_KEY2 not in run.output
    assert keys["key2"] not in run.output


def test_rotate_key_regenerates_the_inactive_slot_when_key2_is_active(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    account, _ = _seed_provisioned_account(deploy_ps_fixture, active_secret_value=DEFAULT_KEY2)

    deploy_ps_fixture.run_deploy("--rotate-key", expect=0)

    keys = _read_keys(deploy_ps_fixture, account)
    assert keys["key2"] == DEFAULT_KEY2
    assert keys["key1"] != DEFAULT_KEY1


def test_rotate_key_skips_config_validation_confirmation_and_preflight(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _seed_provisioned_account(deploy_ps_fixture, active_secret_value=DEFAULT_KEY1)

    deploy_ps_fixture.run_deploy("--rotate-key", expect=0)

    log = deploy_ps_fixture.read_az_log()
    assert not any(line.startswith("role assignment list") for line in log)
    assert not any(line.startswith("cognitiveservices model list") for line in log)
    assert not any(line.startswith("aks create") for line in log)


def test_rotate_key_run_before_any_successful_deploy_fails_clearly(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    """The preflight-guard case (require_account_exists/require_keyvault_exists): a subscription
    that has never had a successful `deploy-ps.sh` run has no AIServices account/Key Vault to
    rotate a key against -- this must fail with an actionable message, not crash on an unset
    variable or an unhandled `az` error.
    """
    deploy_ps_fixture.seed_subscription(id_=SUBSCRIPTION_ID)

    run = deploy_ps_fixture.run_deploy("--rotate-key", expect=1)

    assert "deploy-ps.sh" in run.stderr
    assert "--rotate-key" in run.stderr
