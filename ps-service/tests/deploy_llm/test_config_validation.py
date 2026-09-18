"""Config validation for `scripts/deploy-llm.sh` (AC-BI-001, PLAN.md §5/S1).

`validate_config` must reject a malformed `scripts/llm-defaults.conf` -- named field, named
file, actionable message -- before any `az` call is made (enforced here simply by there being
no fake `az` on `PATH` yet: an invalid-config case that somehow reached an `az` call would fail
with "command not found", not the exit code these tests assert).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conftest import DeployLlmFixture

CONFIG_DISPLAY_PATH = "scripts/llm-defaults.conf"
BASH_SHEBANG = "#!/usr/bin/env bash"

VALID_CONFIG = """# scripts/llm-defaults.conf — evaluator-tunable Azure LLM bootstrap defaults
# (issue #105). No secrets. See docs/architecture/customer-azure-llm-bootstrap.md.

LLM_REGION=swedencentral
LLM_REGION_CANDIDATES=(swedencentral francecentral westeurope germanywestcentral)
LLM_CHAT_MODEL_NAME=gpt-5.4-mini
LLM_CHAT_MODEL_CAPACITY=1000
LLM_EMBED_MODEL_NAME=text-embedding-3-large
LLM_EMBED_MODEL_CAPACITY=350
"""


def _write_config(fixture: DeployLlmFixture, contents: str) -> None:
    fixture.config_path.write_text(contents, encoding="utf-8")


def test_rejects_non_eu_region_naming_field_and_file(deploy_llm_fixture: DeployLlmFixture) -> None:
    _write_config(
        deploy_llm_fixture,
        VALID_CONFIG.replace("(swedencentral francecentral", "(eastus francecentral", 1),
    )

    run = deploy_llm_fixture.run_deploy(expect=1)

    assert (
        f'{CONFIG_DISPLAY_PATH}: LLM_REGION_CANDIDATES[0] "eastus" is not one of the supported '
        "EU regions: swedencentral, francecentral, westeurope, germanywestcentral" in run.stderr
    )


def test_rejects_region_typo(deploy_llm_fixture: DeployLlmFixture) -> None:
    _write_config(
        deploy_llm_fixture,
        VALID_CONFIG.replace("francecentral westeurope", "francecentre westeurope", 1),
    )

    run = deploy_llm_fixture.run_deploy(expect=1)

    assert (
        f'{CONFIG_DISPLAY_PATH}: LLM_REGION_CANDIDATES[1] "francecentre" is not one of the '
        "supported EU regions: swedencentral, francecentral, westeurope, germanywestcentral"
        in run.stderr
    )


def test_rejects_non_positive_chat_capacity(deploy_llm_fixture: DeployLlmFixture) -> None:
    _write_config(
        deploy_llm_fixture,
        VALID_CONFIG.replace("LLM_CHAT_MODEL_CAPACITY=1000", "LLM_CHAT_MODEL_CAPACITY=0", 1),
    )

    run = deploy_llm_fixture.run_deploy(expect=1)

    assert (
        f'{CONFIG_DISPLAY_PATH}: LLM_CHAT_MODEL_CAPACITY "0" must be a positive integer'
        in run.stderr
    )


def test_rejects_non_positive_embed_capacity(deploy_llm_fixture: DeployLlmFixture) -> None:
    _write_config(
        deploy_llm_fixture,
        VALID_CONFIG.replace("LLM_EMBED_MODEL_CAPACITY=350", "LLM_EMBED_MODEL_CAPACITY=-5", 1),
    )

    run = deploy_llm_fixture.run_deploy(expect=1)

    assert (
        f'{CONFIG_DISPLAY_PATH}: LLM_EMBED_MODEL_CAPACITY "-5" must be a positive integer'
        in run.stderr
    )


def test_rejects_empty_chat_model_name(deploy_llm_fixture: DeployLlmFixture) -> None:
    _write_config(
        deploy_llm_fixture,
        VALID_CONFIG.replace("LLM_CHAT_MODEL_NAME=gpt-5.4-mini", 'LLM_CHAT_MODEL_NAME="   "', 1),
    )

    run = deploy_llm_fixture.run_deploy(expect=1)

    assert f"{CONFIG_DISPLAY_PATH}: LLM_CHAT_MODEL_NAME must not be empty" in run.stderr


def test_rejects_empty_embed_model_name(deploy_llm_fixture: DeployLlmFixture) -> None:
    _write_config(
        deploy_llm_fixture,
        VALID_CONFIG.replace(
            "LLM_EMBED_MODEL_NAME=text-embedding-3-large", 'LLM_EMBED_MODEL_NAME=""', 1
        ),
    )

    run = deploy_llm_fixture.run_deploy(expect=1)

    assert f"{CONFIG_DISPLAY_PATH}: LLM_EMBED_MODEL_NAME must not be empty" in run.stderr


def test_valid_config_does_not_exit_on_validation(deploy_llm_fixture: DeployLlmFixture) -> None:
    _write_config(deploy_llm_fixture, VALID_CONFIG)
    # A valid config proceeds past validate_config() into the naming/confirmation-table step
    # (S2), which needs a subscription id to compute names -- seeded here so this test keeps
    # verifying "validation itself never blocks a valid config", not an unrelated az failure.
    deploy_llm_fixture.seed_subscription()

    run = deploy_llm_fixture.run_deploy(expect=None)

    assert run.returncode != 1, run.output
    # `log_step` unconditionally announces "==> Loading and validating {CONFIG_DISPLAY_PATH}" at
    # startup (issue #110), so CONFIG_DISPLAY_PATH alone always appears in output -- a
    # fail_validation message is distinguished by the trailing colon (`"%s: %s\n"`).
    assert f"{CONFIG_DISPLAY_PATH}:" not in run.output


def test_deploy_llm_sh_has_bash_shebang_executable_bit_and_strict_mode() -> None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "deploy-llm.sh"
    assert os.access(script, os.X_OK), f"{script} is not executable"
    lines = script.read_text(encoding="utf-8").splitlines()
    assert lines[0] == BASH_SHEBANG, f"deploy-llm.sh does not start with `{BASH_SHEBANG}`"
    assert "set -euo pipefail" in lines, "deploy-llm.sh does not enable strict mode"
