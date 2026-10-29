"""Tests that the pinned third-party guardrail tools are declared and run in `trunk-worthy`.

The unused-symbol and import-layering checks are third-party tools pinned in the uv dev group,
so they need no network access in this test tier: the tests read `pyproject.toml`, the
`.insitu.yml` inventory and the tool configuration, and fail when a pin, a command or a
suppression justification drifts.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
INSITU_PATH = REPO_ROOT / ".insitu.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
SERVICE_SOURCE_ROOTS = ("ps-service/src", "ps-cli/src", "ps-test-support/src", "scripts")
EXPECTED_PINS = {"unused-symbols": "vulture==2.16", "import-layering": "import-linter==2.15"}
EXPECTED_COMMANDS = {"unused-symbols": "uv run vulture", "import-layering": "uv run lint-imports"}
SUPPRESSION_PATTERN = re.compile(r"# noqa: V10\d - \S")
BARE_SUPPRESSION_PATTERN = re.compile(r"# noqa: V10\d\s*$")


def _yaml(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _pyproject() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _insitu_check(check_id: str) -> dict[str, Any]:
    checks = [c for c in _yaml(INSITU_PATH)["inventory"] if c.get("id") == check_id]
    assert len(checks) == 1, f"CI check {check_id} must be declared exactly once"
    return cast("dict[str, Any]", checks[0])


def _wave_checks(wave_id: str) -> list[str]:
    waves = [w for w in _yaml(INSITU_PATH)["waves"] if w.get("id") == wave_id]
    assert len(waves) == 1
    return cast("list[str]", waves[0]["checks"])


@pytest.mark.parametrize(("check_id", "pin"), sorted(EXPECTED_PINS.items()))
def test_dev_group_pins_the_tool(check_id: str, pin: str) -> None:
    assert pin in _pyproject()["dependency-groups"]["dev"], check_id


@pytest.mark.parametrize("check_id", sorted(EXPECTED_COMMANDS))
def test_ci_runs_the_tool_directly_over_the_whole_tree(check_id: str) -> None:
    assert _insitu_check(check_id)["command"].strip() == EXPECTED_COMMANDS[check_id]


@pytest.mark.parametrize("check_id", sorted(EXPECTED_PINS))
def test_trunk_worthy_wave_lists_the_tool_check_and_fix_all_does_not(check_id: str) -> None:
    assert check_id in _wave_checks("trunk-worthy")
    assert check_id not in _wave_checks("fix-all")


def test_vulture_config_gates_at_high_confidence_with_no_global_name_ignore() -> None:
    config = _pyproject()["tool"]["vulture"]

    assert config["min_confidence"] == 80
    assert "ignore_names" not in config
    assert "ignore_decorators" not in config
    assert sorted(config["paths"]) == sorted(SERVICE_SOURCE_ROOTS)


def test_every_vulture_suppression_in_source_carries_a_reason() -> None:
    bare: list[str] = []
    seen = 0
    for root in SERVICE_SOURCE_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "# noqa: V10" not in line:
                    continue
                seen += 1
                if BARE_SUPPRESSION_PATTERN.search(line) or not SUPPRESSION_PATTERN.search(line):
                    bare.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert seen > 0
    assert not bare, f"vulture suppressions without a reason: {bare}"


def test_ruff_treats_vulture_codes_as_external_so_suppressions_are_not_flagged() -> None:
    external = _pyproject()["tool"]["ruff"]["lint"]["external"]

    assert {"V104", "V107"} <= set(external)


def test_import_contracts_cover_service_cli_foundations_and_entry_layer() -> None:
    config = _pyproject()["tool"]["importlinter"]
    ids = {contract["id"] for contract in config["contracts"]}

    assert ids == {"cli-not-service", "service-not-cli", "leaf-foundations", "entry-layer"}
    assert config["include_external_packages"] is False
    assert set(config["root_packages"]) == {"ps_service", "ps_cli", "ps_test_support"}


def test_import_layering_baseline_only_ignores_edges_into_the_api_package() -> None:
    contracts = {c["id"]: c for c in _pyproject()["tool"]["importlinter"]["contracts"]}
    ignored = contracts["entry-layer"]["ignore_imports"]

    assert ignored, "the entry layer baseline must list the known edges"
    for edge in ignored:
        assert re.fullmatch(r"ps_service\.\w+(\.\w+)* -> ps_service\.api\.\w+", edge), edge
    for other in ("cli-not-service", "service-not-cli", "leaf-foundations"):
        assert "ignore_imports" not in contracts[other]
