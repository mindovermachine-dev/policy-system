"""`--rotate-key` mode: active-slot detection + regenerate-the-inactive-key (AC-BI-014,
AC-BI-013 second enforcement; PLAN.md §5/S10, §0.6).

Branches immediately after flag parsing -- skips config validation, the confirmation table, RBAC
preflight, and region/quota selection entirely, none of which matter for rotating an
already-provisioned account's key.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture

SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
DEFAULT_KEY1 = "FAKE-KEY-1-INITIAL"
DEFAULT_KEY2 = "FAKE-KEY-2-INITIAL"


def _hash8(subscription_id: str) -> str:
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:8]


def _names(subscription_id: str = SUBSCRIPTION_ID) -> tuple[str, str]:
    hash8 = _hash8(subscription_id)
    return f"policy-system-llm-{hash8}", f"kv-ps-llm-{hash8}"


def _seed_provisioned_account(
    fixture: DeployLlmFixture, *, active_secret_value: str
) -> tuple[str, str]:
    """Seeds an existing account/keyvault with a stored AZURE-API-KEY secret matching
    <active_secret_value> -- whichever of key1/key2 that equals is the "active" slot §0.6 says
    rotation must leave untouched.
    """
    fixture.seed_subscription(id_=SUBSCRIPTION_ID)
    account, vault = _names()
    fixture.seed_existing_account(account, key1=DEFAULT_KEY1, key2=DEFAULT_KEY2)
    fixture.seed_existing_keyvault(vault)
    fixture.seed_existing_secret(vault, "AZURE-API-KEY", active_secret_value)
    return account, vault


def _read_keys(fixture: DeployLlmFixture, account: str) -> dict[str, str]:
    path = fixture.azure_state / "accounts" / f"{account}-keys.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_rotate_key_regenerates_the_inactive_slot_when_key1_is_active(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    account, _ = _seed_provisioned_account(deploy_llm_fixture, active_secret_value=DEFAULT_KEY1)

    deploy_llm_fixture.run_deploy("--rotate-key", expect=0)

    keys = _read_keys(deploy_llm_fixture, account)
    assert keys["key1"] == DEFAULT_KEY1
    assert keys["key2"] != DEFAULT_KEY2


def test_rotate_key_regenerates_the_inactive_slot_when_key2_is_active(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    account, _ = _seed_provisioned_account(deploy_llm_fixture, active_secret_value=DEFAULT_KEY2)

    deploy_llm_fixture.run_deploy("--rotate-key", expect=0)

    keys = _read_keys(deploy_llm_fixture, account)
    assert keys["key2"] == DEFAULT_KEY2
    assert keys["key1"] != DEFAULT_KEY1


def test_rotate_key_writes_new_value_to_keyvault_secret(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    account, vault = _seed_provisioned_account(deploy_llm_fixture, active_secret_value=DEFAULT_KEY1)

    deploy_llm_fixture.run_deploy("--rotate-key", expect=0)

    keys = _read_keys(deploy_llm_fixture, account)
    assert deploy_llm_fixture.read_secret(vault, "AZURE-API-KEY") == keys["key2"]


def test_rotate_key_leaves_the_previously_active_key_value_reachable_via_keys_list(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    account, _ = _seed_provisioned_account(deploy_llm_fixture, active_secret_value=DEFAULT_KEY1)

    deploy_llm_fixture.run_deploy("--rotate-key", expect=0)

    keys = _read_keys(deploy_llm_fixture, account)
    assert keys["key1"] == DEFAULT_KEY1


def test_rotate_key_skips_config_validation_confirmation_and_preflight(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    _seed_provisioned_account(deploy_llm_fixture, active_secret_value=DEFAULT_KEY1)

    deploy_llm_fixture.run_deploy("--rotate-key", expect=0)

    log = deploy_llm_fixture.read_az_log()
    assert not any(line.startswith("role assignment list") for line in log)
    assert not any(line.startswith("cognitiveservices model list") for line in log)


def test_rotate_key_fails_clearly_when_account_does_not_exist_yet(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    deploy_llm_fixture.seed_subscription(id_=SUBSCRIPTION_ID)

    run = deploy_llm_fixture.run_deploy("--rotate-key", expect=1)

    assert "deploy-llm.sh" in run.stderr


def test_rotate_key_output_never_contains_old_or_new_key_values(
    deploy_llm_fixture: DeployLlmFixture,
) -> None:
    account, _ = _seed_provisioned_account(deploy_llm_fixture, active_secret_value=DEFAULT_KEY1)

    run = deploy_llm_fixture.run_deploy("--rotate-key", expect=0)

    keys = _read_keys(deploy_llm_fixture, account)
    assert DEFAULT_KEY1 not in run.output
    assert keys["key2"] not in run.output
