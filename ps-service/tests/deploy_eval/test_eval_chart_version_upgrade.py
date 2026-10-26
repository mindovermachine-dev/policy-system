"""`scripts/deploy-ps-eval.sh` decides on the chart version as well as the values.

A release bundle carries a `VERSION` file; re-running the script from a newer bundle upgrades the
release to that chart version, an equal version with unchanged values is a no-op, and a clone or
a local chart path decides on the values alone.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from conftest import EvalFixture, ScriptRun

OWNER_EMAIL = "owner@example.com"
PUBLISHED_CHART_REF = "oci://ghcr.io/mindovermachine-dev/charts/policy-system"
LOCAL_CHART_REF = "./charts/policy-system"


def _install_at(fixture: EvalFixture, chart_version: str, chart_ref: str) -> None:
    """Install once with the script's own values, then record the deployed chart version."""
    fixture.run("--owner-email", OWNER_EMAIL, PS_CHART_REF=chart_ref)
    (fixture.helm_state / "chart-version").write_text(chart_version, encoding="utf-8")


def _list_calls(fixture: EvalFixture) -> list[str]:
    return [call for call in fixture.helm_calls() if call.startswith("list")]


def test_eval_newer_bundle_version_runs_one_helm_upgrade_pinned_to_the_bundle_version(
    eval_fixture: EvalFixture,
) -> None:
    _install_at(eval_fixture, "12.0.1", chart_ref=PUBLISHED_CHART_REF)
    eval_fixture.write_bundle_version("12.1.0")
    upgrades_before = len(eval_fixture.helm_upgrades())

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_CHART_REF=PUBLISHED_CHART_REF)

    new_upgrades = eval_fixture.helm_upgrades()[upgrades_before:]
    assert len(new_upgrades) == 1
    assert "--version 12.1.0" in new_upgrades[0]
    assert "Helm release policy-system unchanged" not in run.stderr


def test_eval_equal_bundle_version_with_unchanged_values_makes_no_helm_upgrade_and_prints_unchanged(
    eval_fixture: EvalFixture,
) -> None:
    _install_at(eval_fixture, "12.1.0", chart_ref=PUBLISHED_CHART_REF)
    eval_fixture.write_bundle_version("12.1.0")
    upgrades_before = len(eval_fixture.helm_upgrades())

    run = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_CHART_REF=PUBLISHED_CHART_REF)

    assert len(eval_fixture.helm_upgrades()) == upgrades_before
    assert "Helm release policy-system unchanged" in run.stderr


def test_eval_without_version_file_decides_on_values_alone(eval_fixture: EvalFixture) -> None:
    _install_at(eval_fixture, "12.0.1", chart_ref=PUBLISHED_CHART_REF)

    unchanged = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_CHART_REF=PUBLISHED_CHART_REF)
    assert len(eval_fixture.helm_upgrades()) == 1
    assert "Helm release policy-system unchanged" in unchanged.stderr

    eval_fixture.run("--owner-email", "other@example.com", PS_CHART_REF=PUBLISHED_CHART_REF)
    upgrades = eval_fixture.helm_upgrades()
    assert len(upgrades) == 2
    assert "--version" not in upgrades[1]
    assert _list_calls(eval_fixture) == []


def test_eval_local_chart_path_ignores_version_file(eval_fixture: EvalFixture) -> None:
    _install_at(eval_fixture, "12.0.1", chart_ref=LOCAL_CHART_REF)
    eval_fixture.write_bundle_version("12.1.0")

    unchanged = eval_fixture.run("--owner-email", OWNER_EMAIL, PS_CHART_REF=LOCAL_CHART_REF)
    assert len(eval_fixture.helm_upgrades()) == 1
    assert "Helm release policy-system unchanged" in unchanged.stderr

    eval_fixture.run("--owner-email", "other@example.com", PS_CHART_REF=LOCAL_CHART_REF)
    upgrades = eval_fixture.helm_upgrades()
    assert len(upgrades) == 2
    assert "--version" not in upgrades[1]
    assert _list_calls(eval_fixture) == []


MUTATING_KUBECTL = re.compile(
    r"^(apply|create|annotate|label|patch|delete|replace|scale|rollout)\b"
)


class _Baseline:
    """Log positions before the run under test, so only that run's calls are inspected."""

    def __init__(self, fixture: EvalFixture) -> None:
        self.helm = len(fixture.helm_calls())
        self.kubectl = len(fixture.kubectl_calls())


def _assert_refused_without_cluster_change(
    fixture: EvalFixture, baseline: _Baseline, run: ScriptRun
) -> None:
    assert run.returncode == 1
    assert "Preparing local TLS" not in run.stderr
    assert not [line for line in fixture.helm_calls()[baseline.helm :] if "upgrade" in line]
    assert not [
        line for line in fixture.kubectl_calls()[baseline.kubectl :] if MUTATING_KUBECTL.match(line)
    ]


def test_eval_older_bundle_version_exits_1_naming_both_versions_and_makes_no_cluster_change(
    eval_fixture: EvalFixture,
) -> None:
    _install_at(eval_fixture, "12.2.0", chart_ref=PUBLISHED_CHART_REF)
    eval_fixture.write_bundle_version("12.1.0")
    baseline = _Baseline(eval_fixture)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, PS_CHART_REF=PUBLISHED_CHART_REF, expect=None
    )

    _assert_refused_without_cluster_change(eval_fixture, baseline, run)
    assert "12.2.0" in run.stderr
    assert "12.1.0" in run.stderr
    assert "policy-system" in run.stderr


def test_eval_older_bundle_version_compares_parts_as_numbers(eval_fixture: EvalFixture) -> None:
    _install_at(eval_fixture, "12.10.0", chart_ref=PUBLISHED_CHART_REF)
    eval_fixture.write_bundle_version("12.9.0")
    baseline = _Baseline(eval_fixture)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, PS_CHART_REF=PUBLISHED_CHART_REF, expect=None
    )

    _assert_refused_without_cluster_change(eval_fixture, baseline, run)
    assert "12.10.0" in run.stderr
    assert "12.9.0" in run.stderr


@pytest.mark.parametrize("content", ["12.1", "v12.1.0", "12.1.0-rc1", "", "abc"])
def test_eval_malformed_bundle_version_exits_1_naming_the_file_before_any_tool_call(
    eval_fixture: EvalFixture, content: str
) -> None:
    eval_fixture.write_bundle_version(content)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, PS_CHART_REF=PUBLISHED_CHART_REF, expect=None
    )

    assert run.returncode == 1
    assert "VERSION" in run.stderr
    assert eval_fixture.helm_calls() == []
    assert eval_fixture.kubectl_calls() == []


def test_eval_unreadable_deployed_chart_version_exits_1_naming_release_and_helm_command(
    eval_fixture: EvalFixture,
) -> None:
    _install_at(eval_fixture, "12.0.1", chart_ref=PUBLISHED_CHART_REF)
    eval_fixture.write_bundle_version("12.1.0")
    baseline = _Baseline(eval_fixture)

    run = eval_fixture.run(
        "--owner-email",
        OWNER_EMAIL,
        PS_CHART_REF=PUBLISHED_CHART_REF,
        PS_TEST_HELM_LIST_FAIL="1",
        expect=None,
    )

    _assert_refused_without_cluster_change(eval_fixture, baseline, run)
    assert "policy-system" in run.stderr
    assert "helm list -f ^policy-system$ -o json" in run.stderr
    assert any("-f ^policy-system$ -o json" in line for line in _list_calls(eval_fixture))


def test_eval_unparseable_deployed_chart_version_exits_1_naming_release_value_and_command(
    eval_fixture: EvalFixture,
) -> None:
    _install_at(eval_fixture, "0.0.0-dev", chart_ref=PUBLISHED_CHART_REF)
    eval_fixture.write_bundle_version("12.1.0")
    baseline = _Baseline(eval_fixture)

    run = eval_fixture.run(
        "--owner-email", OWNER_EMAIL, PS_CHART_REF=PUBLISHED_CHART_REF, expect=None
    )

    _assert_refused_without_cluster_change(eval_fixture, baseline, run)
    assert "policy-system" in run.stderr
    assert "0.0.0-dev" in run.stderr
    assert "helm list -f ^policy-system$ -o json" in run.stderr
