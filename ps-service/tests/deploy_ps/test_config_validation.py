"""Config validation for `scripts/deploy-ps.sh` (contributes AC-BI-019; PLAN.md §5/S5).

`validate_config` must reject a malformed `scripts/ps-defaults.conf` -- named field, named
file, actionable message -- before any `az` call is made (enforced here simply by there being
no fake `az` on `PATH` yet: an invalid-config case that somehow reached an `az` call would fail
with "command not found", not the exit code these tests assert).

Unlike `scripts/deploy-llm.sh`, `ps-defaults.conf` has no single `LLM_REGION` field (only
`LLM_REGION_CANDIDATES` -- region *selection* is a later slice, S8) and adds a SKU field per
model (`LLM_CHAT_MODEL_SKU`/`LLM_EMBED_MODEL_SKU`, PLAN.md §1) since these SKUs are
evaluator-tunable here, unlike `deploy-llm.sh`'s hardcoded literals.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployPsFixture

CONFIG_DISPLAY_PATH = "scripts/ps-defaults.conf"
BASH_SHEBANG = "#!/usr/bin/env bash"

VALID_CONFIG = """# scripts/ps-defaults.conf — evaluator-tunable Azure customer-tenant deployment
# defaults (issue #111). No secrets. See docs/architecture/customer-azure-deployment.md.

LLM_REGION_CANDIDATES=(swedencentral francecentral westeurope germanywestcentral)
LLM_CHAT_MODEL_NAME="gpt-5.4-mini"
LLM_CHAT_MODEL_SKU="DataZoneStandard"
LLM_CHAT_MODEL_CAPACITY=200
LLM_EMBED_MODEL_NAME="text-embedding-3-large"
LLM_EMBED_MODEL_SKU="Standard"
LLM_EMBED_MODEL_CAPACITY=350
TLS_CONTACT_EMAIL="tls-contact@example.test"
"""


def _write_config(fixture: DeployPsFixture, contents: str) -> None:
    fixture.config_path.write_text(contents, encoding="utf-8")


def test_rejects_non_eu_region_naming_field_and_file(deploy_ps_fixture: DeployPsFixture) -> None:
    _write_config(
        deploy_ps_fixture,
        VALID_CONFIG.replace("(swedencentral francecentral", "(eastus francecentral", 1),
    )

    run = deploy_ps_fixture.run_deploy(expect=1)

    assert (
        f'{CONFIG_DISPLAY_PATH}: LLM_REGION_CANDIDATES[0] "eastus" is not one of the supported '
        "EU regions: swedencentral, francecentral, westeurope, germanywestcentral" in run.stderr
    )


def test_rejects_region_typo(deploy_ps_fixture: DeployPsFixture) -> None:
    _write_config(
        deploy_ps_fixture,
        VALID_CONFIG.replace("francecentral westeurope", "francecentre westeurope", 1),
    )

    run = deploy_ps_fixture.run_deploy(expect=1)

    assert (
        f'{CONFIG_DISPLAY_PATH}: LLM_REGION_CANDIDATES[1] "francecentre" is not one of the '
        "supported EU regions: swedencentral, francecentral, westeurope, germanywestcentral"
        in run.stderr
    )


def test_rejects_non_positive_chat_capacity(deploy_ps_fixture: DeployPsFixture) -> None:
    _write_config(
        deploy_ps_fixture,
        VALID_CONFIG.replace("LLM_CHAT_MODEL_CAPACITY=200", "LLM_CHAT_MODEL_CAPACITY=0", 1),
    )

    run = deploy_ps_fixture.run_deploy(expect=1)

    assert (
        f'{CONFIG_DISPLAY_PATH}: LLM_CHAT_MODEL_CAPACITY "0" must be a positive integer'
        in run.stderr
    )


def test_rejects_non_positive_embed_capacity(deploy_ps_fixture: DeployPsFixture) -> None:
    _write_config(
        deploy_ps_fixture,
        VALID_CONFIG.replace("LLM_EMBED_MODEL_CAPACITY=350", "LLM_EMBED_MODEL_CAPACITY=-5", 1),
    )

    run = deploy_ps_fixture.run_deploy(expect=1)

    assert (
        f'{CONFIG_DISPLAY_PATH}: LLM_EMBED_MODEL_CAPACITY "-5" must be a positive integer'
        in run.stderr
    )


def test_rejects_empty_chat_model_name(deploy_ps_fixture: DeployPsFixture) -> None:
    _write_config(
        deploy_ps_fixture,
        VALID_CONFIG.replace('LLM_CHAT_MODEL_NAME="gpt-5.4-mini"', 'LLM_CHAT_MODEL_NAME="   "', 1),
    )

    run = deploy_ps_fixture.run_deploy(expect=1)

    assert f"{CONFIG_DISPLAY_PATH}: LLM_CHAT_MODEL_NAME must not be empty" in run.stderr


def test_rejects_empty_embed_model_name(deploy_ps_fixture: DeployPsFixture) -> None:
    _write_config(
        deploy_ps_fixture,
        VALID_CONFIG.replace(
            'LLM_EMBED_MODEL_NAME="text-embedding-3-large"', 'LLM_EMBED_MODEL_NAME=""', 1
        ),
    )

    run = deploy_ps_fixture.run_deploy(expect=1)

    assert f"{CONFIG_DISPLAY_PATH}: LLM_EMBED_MODEL_NAME must not be empty" in run.stderr


def test_rejects_empty_chat_model_sku(deploy_ps_fixture: DeployPsFixture) -> None:
    _write_config(
        deploy_ps_fixture,
        VALID_CONFIG.replace('LLM_CHAT_MODEL_SKU="DataZoneStandard"', 'LLM_CHAT_MODEL_SKU=""', 1),
    )

    run = deploy_ps_fixture.run_deploy(expect=1)

    assert f"{CONFIG_DISPLAY_PATH}: LLM_CHAT_MODEL_SKU must not be empty" in run.stderr


def test_rejects_empty_embed_model_sku(deploy_ps_fixture: DeployPsFixture) -> None:
    _write_config(
        deploy_ps_fixture,
        VALID_CONFIG.replace('LLM_EMBED_MODEL_SKU="Standard"', 'LLM_EMBED_MODEL_SKU="   "', 1),
    )

    run = deploy_ps_fixture.run_deploy(expect=1)

    assert f"{CONFIG_DISPLAY_PATH}: LLM_EMBED_MODEL_SKU must not be empty" in run.stderr


def test_valid_config_does_not_exit_on_validation(deploy_ps_fixture: DeployPsFixture) -> None:
    _write_config(deploy_ps_fixture, VALID_CONFIG)
    # A valid, fully-filled-in config (TLS_CONTACT_EMAIL included) proceeds past validate_config()
    # into the naming/confirmation-table step, which needs a subscription id to compute names --
    # seeded here so this test keeps verifying "validation itself never blocks a valid config",
    # not an unrelated az failure.
    deploy_ps_fixture.seed_subscription()

    run = deploy_ps_fixture.run_deploy(expect=None, stdin="N\n")

    assert run.returncode != 1, run.output
    # `log_step` unconditionally announces "==> Loading and validating {CONFIG_DISPLAY_PATH}" at
    # startup, so CONFIG_DISPLAY_PATH alone always appears in output -- a fail_validation message
    # is distinguished by the trailing colon (`"%s: %s\n"`).
    assert f"{CONFIG_DISPLAY_PATH}:" not in run.output


def test_blank_tls_contact_email_prompts_interactively(deploy_ps_fixture: DeployPsFixture) -> None:
    _write_config(
        deploy_ps_fixture,
        VALID_CONFIG.replace(
            'TLS_CONTACT_EMAIL="tls-contact@example.test"', 'TLS_CONTACT_EMAIL=""', 1
        ),
    )
    deploy_ps_fixture.seed_subscription()

    # First stdin line answers the TLS_CONTACT_EMAIL prompt; second declines the confirmation
    # table's [Y/n] prompt, so this test exercises only the TLS prompt itself, in isolation.
    run = deploy_ps_fixture.run_deploy(stdin="prompted@example.test\nN\n", expect=0)

    assert "Contact email for Let's Encrypt certificate notices (TLS_CONTACT_EMAIL): " in run.stdout
    # A blank TLS_CONTACT_EMAIL that never got prompted for would instead fail validate_config
    # with a "must not be empty" error -- reaching the decline message proves the prompted value
    # was accepted and validation passed.
    assert f"{CONFIG_DISPLAY_PATH}: TLS_CONTACT_EMAIL must not be empty" not in run.stderr


def test_deploy_ps_sh_has_bash_shebang_executable_bit_and_strict_mode() -> None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "deploy-ps.sh"
    assert os.access(script, os.X_OK), f"{script} is not executable"
    lines = script.read_text(encoding="utf-8").splitlines()
    assert lines[0] == BASH_SHEBANG, f"deploy-ps.sh does not start with `{BASH_SHEBANG}`"
    assert "set -euo pipefail" in lines, "deploy-ps.sh does not enable strict mode"
