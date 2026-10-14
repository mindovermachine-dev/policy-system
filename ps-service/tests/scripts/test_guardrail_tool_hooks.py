"""Tests that the pinned third-party guardrail hooks are declared and run in CI.

The unused-symbol and import-layering checks are third-party tools run through pre-commit with
pinned versions, so they need no network access in this test tier: the tests read the hook
declarations, the CI inventory and the tool configuration, and fail when a pin, a hook, a CI
command or a suppression justification drifts.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PRECOMMIT_PATH = REPO_ROOT / ".pre-commit-config.yaml"
INSITU_PATH = REPO_ROOT / ".insitu.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
SERVICE_SOURCE_ROOTS = ("ps-service/src", "ps-cli/src", "ps-test-support/src", "scripts")
EXPECTED_PINS = {"unused-symbols": "vulture==2.16", "import-layering": "import-linter==2.3"}
SUPPRESSION_PATTERN = re.compile(r"# noqa: V10\d - \S")
BARE_SUPPRESSION_PATTERN = re.compile(r"# noqa: V10\d\s*$")


def _yaml(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")))


def _pyproject() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))


def _hook(hook_id: str) -> dict[str, Any]:
    hooks = [
        hook
        for repo in _yaml(PRECOMMIT_PATH)["repos"]
        for hook in repo["hooks"]
        if hook["id"] == hook_id
    ]
    assert len(hooks) == 1, f"hook {hook_id} must be declared exactly once"
    return cast("dict[str, Any]", hooks[0])


def _insitu_check(check_id: str) -> dict[str, Any]:
    checks = [c for c in _yaml(INSITU_PATH)["inventory"] if c.get("id") == check_id]
    assert len(checks) == 1, f"CI check {check_id} must be declared exactly once"
    return cast("dict[str, Any]", checks[0])


def _wave_checks(wave_id: str) -> list[str]:
    waves = [w for w in _yaml(INSITU_PATH)["waves"] if w.get("id") == wave_id]
    assert len(waves) == 1
    return cast("list[str]", waves[0]["checks"])


@pytest.mark.parametrize(("hook_id", "pin"), sorted(EXPECTED_PINS.items()))
def test_tool_hook_builds_a_pinned_environment_without_staged_file_arguments(
    hook_id: str, pin: str
) -> None:
    hook = _hook(hook_id)

    assert hook["language"] == "python"
    assert hook["additional_dependencies"] == [pin]
    assert hook["pass_filenames"] is False


@pytest.mark.parametrize("hook_id", sorted(EXPECTED_PINS))
def test_ci_runs_the_same_hook_over_all_files(hook_id: str) -> None:
    command = _insitu_check(hook_id)["command"]

    assert command.strip() == f"uv run pre-commit run {hook_id} --all-files"


@pytest.mark.parametrize("hook_id", sorted(EXPECTED_PINS))
def test_trunk_worthy_wave_lists_the_tool_check_and_fix_all_does_not(hook_id: str) -> None:
    assert hook_id in _wave_checks("trunk-worthy")
    assert hook_id not in _wave_checks("fix-all")


@pytest.mark.parametrize(
    ("hook_id", "staged", "expected"),
    [
        ("unused-symbols", "ps-service/src/ps_service/api/app.py", True),
        ("unused-symbols", "scripts/check_doc_links.py", True),
        ("unused-symbols", "pyproject.toml", True),
        ("unused-symbols", "ps-service/tests/api/test_app.py", False),
        ("import-layering", "ps-cli/src/ps_cli/main.py", True),
        ("import-layering", "pyproject.toml", True),
        ("import-layering", "README.md", False),
    ],
)
def test_hook_file_filter_selects_only_files_that_can_change_the_result(
    hook_id: str, staged: str, *, expected: bool
) -> None:
    assert bool(re.search(_hook(hook_id)["files"], staged)) is expected


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


def test_import_layering_hook_sets_source_roots_and_leaves_no_cache_or_bytecode() -> None:
    entry = _hook("import-layering")["entry"]

    assert "PYTHONPATH=ps-service/src:ps-cli/src:ps-test-support/src" in entry
    assert "PYTHONDONTWRITEBYTECODE=1" in entry
    assert "--no-cache" in entry
