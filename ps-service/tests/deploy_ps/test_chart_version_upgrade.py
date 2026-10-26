"""`scripts/deploy-ps-prod.sh` decides on the chart version as well as the values.

A release bundle carries a `VERSION` file; re-running the script from a newer bundle upgrades the
release to that chart version, an equal version with unchanged values is a no-op, and a clone or
a local chart path decides on the values alone.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from conftest import DeployPsFixture, ScriptRun

HELM_RELEASE_NAME = "policy-system"
LOCAL_CHART_REF = "./charts/policy-system"


def _install_at(fixture: DeployPsFixture, chart_version: str, **env: str) -> None:
    """Install once with the script's own values, then record the deployed chart version."""
    fixture.fill_tls_contact_email()
    fixture.seed_subscription()
    fixture.run_deploy("--yes", expect=0, extra_env=env)
    fixture.seed_helm_release(fixture.read_helm_release_values() or {}, chart_version=chart_version)


def _upgrade_calls(fixture: DeployPsFixture) -> list[str]:
    return [
        line
        for line in fixture.read_helm_log()
        if line.startswith(f"upgrade --install {HELM_RELEASE_NAME} ")
    ]


def _list_calls(fixture: DeployPsFixture) -> list[str]:
    return [line for line in fixture.read_helm_log() if line.startswith("list")]


def test_prod_newer_bundle_version_runs_one_helm_upgrade_pinned_to_the_bundle_version(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _install_at(deploy_ps_fixture, "12.0.1")
    deploy_ps_fixture.write_bundle_version("12.1.0")
    upgrades_before = len(_upgrade_calls(deploy_ps_fixture))

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    new_upgrades = _upgrade_calls(deploy_ps_fixture)[upgrades_before:]
    assert len(new_upgrades) == 1
    assert "--version 12.1.0" in new_upgrades[0]


def test_prod_equal_bundle_version_with_unchanged_values_makes_no_helm_upgrade(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _install_at(deploy_ps_fixture, "12.1.0")
    deploy_ps_fixture.write_bundle_version("12.1.0")
    upgrades_before = len(_upgrade_calls(deploy_ps_fixture))

    deploy_ps_fixture.run_deploy("--yes", expect=0)

    assert len(_upgrade_calls(deploy_ps_fixture)) == upgrades_before


def test_prod_without_version_file_decides_on_values_alone(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _install_at(deploy_ps_fixture, "12.0.1")
    upgrades_before = len(_upgrade_calls(deploy_ps_fixture))

    deploy_ps_fixture.run_deploy("--yes", expect=0)
    assert len(_upgrade_calls(deploy_ps_fixture)) == upgrades_before

    deploy_ps_fixture.seed_helm_release({}, chart_version="12.0.1")
    deploy_ps_fixture.run_deploy("--yes", expect=0)
    changed = _upgrade_calls(deploy_ps_fixture)[upgrades_before:]
    assert len(changed) == 1
    assert "--version" not in changed[0]
    assert _list_calls(deploy_ps_fixture) == []


def test_prod_local_chart_path_ignores_version_file(deploy_ps_fixture: DeployPsFixture) -> None:
    env = {"PS_CHART_REF": LOCAL_CHART_REF}
    _install_at(deploy_ps_fixture, "12.0.1", **env)
    deploy_ps_fixture.write_bundle_version("12.1.0")
    upgrades_before = len(_upgrade_calls(deploy_ps_fixture))

    deploy_ps_fixture.run_deploy("--yes", expect=0, extra_env=env)
    assert len(_upgrade_calls(deploy_ps_fixture)) == upgrades_before

    deploy_ps_fixture.seed_helm_release({}, chart_version="12.0.1")
    deploy_ps_fixture.run_deploy("--yes", expect=0, extra_env=env)
    changed = _upgrade_calls(deploy_ps_fixture)[upgrades_before:]
    assert len(changed) == 1
    assert "--version" not in changed[0]
    assert _list_calls(deploy_ps_fixture) == []


def _set_value(values: dict[str, object], dotted_key: str, value: object) -> None:
    """Set a nested value in a Helm values object, creating the levels on the way."""
    *parents, leaf = dotted_key.split(".")
    node = values
    for parent in parents:
        child = node.setdefault(parent, {})
        assert isinstance(child, dict)
        node = cast("dict[str, object]", child)
    node[leaf] = value


def _get_value(values: dict[str, object] | None, dotted_key: str) -> object:
    """Read a nested value from a Helm values object; None when a level is missing."""
    *parents, leaf = dotted_key.split(".")
    node = values
    for parent in parents:
        child = node.get(parent) if node is not None else None
        node = cast("dict[str, object]", child) if isinstance(child, dict) else None
    return node.get(leaf) if node is not None else None


def _seed_operator_values(fixture: DeployPsFixture, chart_version: str) -> dict[str, object]:
    """Add values the script never sets to the installed release; returns the seeded values."""
    values = fixture.read_helm_release_values() or {}
    _set_value(values, "psService.extra", "operator-extra")
    _set_value(values, "falkordb.persistence.size", "64Gi")
    _set_value(values, "falkordb.persistence.durableStorageClass.name", "operator-class")
    _set_value(values, "llm.operatorNote", "keep-me")
    fixture.seed_helm_release(values, chart_version=chart_version)
    return values


def _write_file_layer(fixture: DeployPsFixture) -> dict[str, str]:
    """Stand in for `values-prod.yaml`, which the fake helm cannot parse."""
    path = fixture.root / "file-values.json"
    path.write_text(
        json.dumps({"falkordb": {"persistence": {"durableStorageClass": {"name": "file-class"}}}}),
        encoding="utf-8",
    )
    return {"PS_TEST_HELM_FILE_VALUES_JSON": str(path)}


def test_prod_version_only_upgrade_preserves_release_values_the_script_does_not_set(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _install_at(deploy_ps_fixture, "12.0.1")
    seeded = _seed_operator_values(deploy_ps_fixture, "12.0.1")
    deploy_ps_fixture.write_bundle_version("12.1.0")
    upgrades_before = len(_upgrade_calls(deploy_ps_fixture))

    deploy_ps_fixture.run_deploy("--yes", extra_env=_write_file_layer(deploy_ps_fixture))

    new_upgrades = _upgrade_calls(deploy_ps_fixture)[upgrades_before:]
    assert len(new_upgrades) == 1
    assert "--reset-then-reuse-values" in new_upgrades[0]
    after = deploy_ps_fixture.read_helm_release_values()
    assert _get_value(after, "psService.extra") == "operator-extra"
    assert _get_value(after, "falkordb.persistence.size") == "64Gi"
    assert _get_value(after, "llm.operatorNote") == "keep-me"
    assert _get_value(after, "psService.auth") == _get_value(seeded, "psService.auth")
    assert _get_value(after, "falkordb.persistence.durableStorageClass.name") == "file-class", (
        "values the file defines are re-applied"
    )


def test_prod_values_change_upgrade_does_not_reuse_prior_release_values(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _install_at(deploy_ps_fixture, "12.0.1")
    values = _seed_operator_values(deploy_ps_fixture, "12.0.1")
    _set_value(values, "ingress.className", "changed")
    deploy_ps_fixture.seed_helm_release(values, chart_version="12.0.1")
    upgrades_before = len(_upgrade_calls(deploy_ps_fixture))

    deploy_ps_fixture.run_deploy("--yes")

    new_upgrades = _upgrade_calls(deploy_ps_fixture)[upgrades_before:]
    assert len(new_upgrades) == 1
    assert "--reset-then-reuse-values" not in new_upgrades[0]
    after = deploy_ps_fixture.read_helm_release_values()
    assert _get_value(after, "psService.extra") is None
    assert _get_value(after, "falkordb.persistence.size") is None


MUTATING_KUBECTL = re.compile(
    r"^(apply|create|annotate|label|patch|delete|replace|scale|rollout)\b"
)


def _new_lines(lines: list[str], already_seen: int) -> list[str]:
    return lines[already_seen:]


class _Baseline:
    """Log positions before the run under test, so only that run's calls are inspected."""

    def __init__(self, fixture: DeployPsFixture) -> None:
        self.helm = len(fixture.read_helm_log())
        self.kubectl = len(fixture.read_kubectl_log())
        self.az = len(fixture.read_az_log())


def _assert_refused_without_cluster_change(
    fixture: DeployPsFixture, baseline: _Baseline, run: ScriptRun
) -> None:
    assert run.returncode == 1
    assert "Syncing LLM credentials" not in run.stderr
    assert not [
        line for line in _new_lines(fixture.read_helm_log(), baseline.helm) if "upgrade" in line
    ]
    assert not [
        line
        for line in _new_lines(fixture.read_kubectl_log(), baseline.kubectl)
        if MUTATING_KUBECTL.match(line)
    ]
    assert not [
        line
        for line in _new_lines(fixture.read_az_log(), baseline.az)
        if line.startswith("role assignment create")
    ]


def test_prod_older_bundle_version_exits_1_naming_both_versions_and_makes_no_cluster_change(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _install_at(deploy_ps_fixture, "12.2.0")
    deploy_ps_fixture.write_bundle_version("12.1.0")
    baseline = _Baseline(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=None)

    _assert_refused_without_cluster_change(deploy_ps_fixture, baseline, run)
    assert "12.2.0" in run.stderr
    assert "12.1.0" in run.stderr
    assert HELM_RELEASE_NAME in run.stderr


def test_prod_older_bundle_version_compares_parts_as_numbers(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _install_at(deploy_ps_fixture, "12.10.0")
    deploy_ps_fixture.write_bundle_version("12.9.0")
    baseline = _Baseline(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=None)

    _assert_refused_without_cluster_change(deploy_ps_fixture, baseline, run)
    assert "12.10.0" in run.stderr
    assert "12.9.0" in run.stderr


@pytest.mark.parametrize("content", ["12.1", "v12.1.0", "12.1.0-rc1", "", "abc"])
def test_prod_malformed_bundle_version_exits_1_naming_the_file_before_any_tool_call(
    deploy_ps_fixture: DeployPsFixture, content: str
) -> None:
    deploy_ps_fixture.fill_tls_contact_email()
    deploy_ps_fixture.seed_subscription()
    deploy_ps_fixture.write_bundle_version(content)

    run = deploy_ps_fixture.run_deploy("--yes", expect=None)

    assert run.returncode == 1
    assert "VERSION" in run.stderr
    assert deploy_ps_fixture.read_az_log() == []
    assert deploy_ps_fixture.read_kubectl_log() == []
    assert deploy_ps_fixture.read_helm_log() == []


def test_prod_unreadable_deployed_chart_version_exits_1_naming_release_and_helm_command(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _install_at(deploy_ps_fixture, "12.0.1")
    deploy_ps_fixture.write_bundle_version("12.1.0")
    baseline = _Baseline(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy(
        "--yes", expect=None, extra_env={"PS_TEST_HELM_LIST_FAIL": "1"}
    )

    _assert_refused_without_cluster_change(deploy_ps_fixture, baseline, run)
    assert HELM_RELEASE_NAME in run.stderr
    assert "helm list -f ^policy-system$ -o json" in run.stderr
    assert any(
        line.startswith("list") and "-f ^policy-system$ -o json" in line
        for line in deploy_ps_fixture.read_helm_log()
    )


def test_prod_unparseable_deployed_chart_version_exits_1_naming_release_value_and_command(
    deploy_ps_fixture: DeployPsFixture,
) -> None:
    _install_at(deploy_ps_fixture, "0.0.0-dev")
    deploy_ps_fixture.write_bundle_version("12.1.0")
    baseline = _Baseline(deploy_ps_fixture)

    run = deploy_ps_fixture.run_deploy("--yes", expect=None)

    _assert_refused_without_cluster_change(deploy_ps_fixture, baseline, run)
    assert HELM_RELEASE_NAME in run.stderr
    assert "0.0.0-dev" in run.stderr
    assert "helm list -f ^policy-system$ -o json" in run.stderr
