"""Chart workload guard: every rendered workload container declares its probes and resources.

The chart is rendered with the real `helm` for the default values and with
`authentik.enabled=true`, so the Authentik subchart's server and worker are held to the same
rules as the chart's own workloads. Rendering needs `helm` and the packaged subchart
(`helm dependency build charts/policy-system`, or `gh insitu run post-create-deps`); without
them this guard fails, and never skips, because a guard that silently does not run proves
nothing. The values passed are dummies that only satisfy the chart's render-time guards; no
credentials, no cluster, no network.

Rules, for the main containers of every Deployment, StatefulSet, DaemonSet, Job and CronJob:

- Deployment, StatefulSet and DaemonSet containers declare a `readinessProbe`, and a
  `startupProbe` if and only if they declare a `livenessProbe`.
- Every main container sets `resources.requests.cpu`, `resources.requests.memory` and
  `resources.limits.memory`.

Exemptions, each with its reason:

- Job and CronJob containers are exempt from the probe rule: they run to completion, nothing
  routes traffic to them, so readiness gates nothing, and a liveness restart would only rerun
  the work.
- Plain init containers are exempt from both rules: they run once, to completion, before any
  main container starts. An init container with `restartPolicy: Always` (a native sidecar)
  runs for the pod's whole life, so it is a main container here.
"""

from __future__ import annotations

import functools
import io
import shutil
import tarfile
from typing import TYPE_CHECKING, Any, NamedTuple, cast

import pytest
import yaml

from guardrails.chart_render import (
    CHART_DIR,
    ChartGuardSetupError,
    render,
    require_helm,
    require_subchart_archives,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

BASE_VALUES = ("llm.provider=ollama", "psService.localTestBypass.enabled=true")
PROFILES: Mapping[str, tuple[str, ...]] = {
    "default": BASE_VALUES,
    "authentik": (*BASE_VALUES, "authentik.enabled=true"),
}

WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"})
"""Kinds whose pods the guard checks; every other rendered kind runs no container."""

PROBED_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet"})
"""Long-running kinds: they serve traffic and must say when they are ready.

A startup probe only holds the liveness probe back during a slow start, so it is required with
a liveness probe; without one it can only kill a slow start (a half-initialised data directory,
a recovery that never finishes), so it is refused alone.
"""

REQUIRED_RESOURCES = ("requests.cpu", "requests.memory", "limits.memory")
"""Resource fields every main container sets: what the scheduler reserves, and the memory cap."""

NO_WORKLOAD = "no Deployment, StatefulSet, DaemonSet, Job or CronJob rendered"

BUILD_HINT_FRAGMENT = "helm dependency build charts/policy-system"


class WorkloadCheck(NamedTuple):
    """What the guard looked at and what it found wrong.

    Attributes:
        checked: `(<Kind>/<name>, <container>)` for every main container the rules ran on.
        problems: One line per violation; holds the no-workload problem when `checked` is empty.
    """

    checked: frozenset[tuple[str, str]]
    problems: list[str]


def _workload_problems(documents: Sequence[Mapping[str, Any]]) -> WorkloadCheck:
    """Hold each main container of each workload in `documents` to the probe and resource rules."""
    checked: set[tuple[str, str]] = set()
    problems: list[str] = []
    for document in documents:
        kind = str(document.get("kind"))
        if kind not in WORKLOAD_KINDS:
            continue
        workload = f"{kind}/{document['metadata']['name']}"
        for container in _main_containers(_pod_spec(document)):
            name = str(container["name"])
            checked.add((workload, name))
            prefix = f"{workload} container {name}"
            if kind in PROBED_KINDS:
                problems.extend(f"{prefix}: {p}" for p in _probe_problems(container))
            problems.extend(f"{prefix}: {p}" for p in _resource_problems(container))
    if not checked:
        problems.append(NO_WORKLOAD)
    return WorkloadCheck(frozenset(checked), problems)


def _field(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return `mapping[key]` when it is a mapping, else an empty one (absent or null)."""
    value: object = mapping.get(key)
    # `isinstance` narrows only to `dict[Unknown, Unknown]`; a rendered manifest has string keys.
    return cast("Mapping[str, Any]", value) if isinstance(value, dict) else {}


def _pod_spec(document: Mapping[str, Any]) -> Mapping[str, Any]:
    spec = _field(document, "spec")
    if document.get("kind") == "CronJob":
        spec = _field(_field(spec, "jobTemplate"), "spec")
    return _field(_field(spec, "template"), "spec")


def _main_containers(pod_spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the containers that run for the pod's life: `containers` and native sidecars."""
    # The Kubernetes pod spec defines `containers` and `initContainers` as lists of container
    # mappings; `helm template` has already rendered them, so the cast states that schema.
    containers = cast("list[Mapping[str, Any]]", pod_spec.get("containers") or [])
    init_containers = cast("list[Mapping[str, Any]]", pod_spec.get("initContainers") or [])
    sidecars = [c for c in init_containers if c.get("restartPolicy") == "Always"]
    return [*containers, *sidecars]


def _probe_problems(container: Mapping[str, Any]) -> list[str]:
    readiness, liveness, startup = (
        bool(container.get(probe)) for probe in ("readinessProbe", "livenessProbe", "startupProbe")
    )
    problems: list[str] = []
    if not readiness:
        problems.append("readiness-probe-missing — no readinessProbe")
    if liveness and not startup:
        problems.append("liveness-without-startup-probe — livenessProbe without startupProbe")
    if startup and not liveness:
        problems.append("startup-without-liveness-probe — startupProbe without livenessProbe")
    return problems


def _resource_problems(container: Mapping[str, Any]) -> list[str]:
    resources = _field(container, "resources")
    problems: list[str] = []
    for field in REQUIRED_RESOURCES:
        section, key = field.split(".")
        value: object = _field(resources, section).get(key)
        if value is None or not str(value).strip():
            problems.append(f"resource-missing — resources.{field} not set")
    return problems


@functools.cache
def _rendered(profile: str) -> tuple[dict[str, Any], ...]:
    helm = require_helm()
    require_subchart_archives(CHART_DIR)
    return tuple(render(helm, CHART_DIR, PROFILES[profile]))


def _container(
    name: str = "app",
    *,
    probes: Sequence[str] = ("readinessProbe",),
    resources: Mapping[str, Mapping[str, str]] | None = None,
    restart_policy: str | None = None,
) -> dict[str, Any]:
    container: dict[str, Any] = {"name": name}
    for probe in probes:
        container[probe] = {"httpGet": {"path": "/readyz", "port": 8080}}
    container["resources"] = (
        {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}}
        if resources is None
        else resources
    )
    if restart_policy is not None:
        container["restartPolicy"] = restart_policy
    return container


def _workload(
    kind: str,
    containers: Sequence[Mapping[str, Any]],
    *,
    name: str = "web",
    init_containers: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    pod_spec: dict[str, Any] = {"containers": list(containers)}
    if init_containers:
        pod_spec["initContainers"] = list(init_containers)
    template = {"spec": pod_spec}
    spec: dict[str, Any] = (
        {"jobTemplate": {"spec": {"template": template}}}
        if kind == "CronJob"
        else {"template": template}
    )
    return {"kind": kind, "metadata": {"name": name}, "spec": spec}


def _tmp_chart(tmp_path: Path) -> Path:
    chart = tmp_path / "chart"
    chart.mkdir()
    for name in ("Chart.yaml", "Chart.lock"):
        shutil.copy(CHART_DIR / name, chart / name)
    return chart


def _authentik_lock_version() -> str:
    lock = yaml.safe_load((CHART_DIR / "Chart.lock").read_text(encoding="utf-8"))
    return next(d["version"] for d in lock["dependencies"] if d["name"] == "authentik")


# --- the rules on synthetic manifests ---


def test_a_deployment_without_probes_is_reported() -> None:
    result = _workload_problems([_workload("Deployment", [_container(probes=())])])

    assert result.problems == [
        "Deployment/web container app: readiness-probe-missing — no readinessProbe"
    ]


def test_a_deployment_with_liveness_but_no_startup_probe_is_reported() -> None:
    container = _container(probes=("readinessProbe", "livenessProbe"))

    result = _workload_problems([_workload("Deployment", [container])])

    assert result.problems == [
        (
            "Deployment/web container app: liveness-without-startup-probe — "
            "livenessProbe without startupProbe"
        )
    ]


def test_a_deployment_with_a_startup_probe_but_no_liveness_probe_is_reported() -> None:
    container = _container(probes=("readinessProbe", "startupProbe"))

    result = _workload_problems([_workload("Deployment", [container])])

    assert result.problems == [
        (
            "Deployment/web container app: startup-without-liveness-probe — "
            "startupProbe without livenessProbe"
        )
    ]


def test_a_readiness_only_deployment_passes() -> None:
    result = _workload_problems([_workload("Deployment", [_container()])])

    assert result.problems == []
    assert result.checked == {("Deployment/web", "app")}


def test_a_deployment_with_readiness_liveness_and_startup_probes_passes() -> None:
    container = _container(probes=("readinessProbe", "livenessProbe", "startupProbe"))

    result = _workload_problems([_workload("Deployment", [container])])

    assert result.problems == []


def test_a_job_without_probes_passes() -> None:
    result = _workload_problems([_workload("Job", [_container(probes=())], name="migrate")])

    assert result.problems == []
    assert result.checked == {("Job/migrate", "app")}


def test_an_init_container_without_resources_passes() -> None:
    init = _container("wait", probes=(), resources={})

    result = _workload_problems([_workload("Deployment", [_container()], init_containers=[init])])

    assert result.problems == []
    assert result.checked == {("Deployment/web", "app")}


@pytest.mark.parametrize("field", REQUIRED_RESOURCES)
def test_a_main_container_missing_a_resource_field_is_reported(field: str) -> None:
    section, key = field.split(".")
    resources = {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}}
    del resources[section][key]

    result = _workload_problems([_workload("Deployment", [_container(resources=resources)])])

    assert result.problems == [
        f"Deployment/web container app: resource-missing — resources.{field} not set"
    ]


def test_an_empty_resource_value_is_reported() -> None:
    resources = {"requests": {"cpu": "", "memory": "128Mi"}, "limits": {"memory": "256Mi"}}

    result = _workload_problems([_workload("Deployment", [_container(resources=resources)])])

    assert result.problems == [
        "Deployment/web container app: resource-missing — resources.requests.cpu not set"
    ]


def test_a_job_container_without_resources_is_reported() -> None:
    job = _workload("Job", [_container(probes=(), resources={})], name="migrate")

    result = _workload_problems([job])

    assert result.problems == [
        f"Job/migrate container app: resource-missing — resources.{field} not set"
        for field in REQUIRED_RESOURCES
    ]


@pytest.mark.parametrize("kind", ["StatefulSet", "DaemonSet"])
def test_statefulset_and_daemonset_are_held_to_the_probe_rules(kind: str) -> None:
    result = _workload_problems([_workload(kind, [_container(probes=())])])

    assert result.problems == [
        f"{kind}/web container app: readiness-probe-missing — no readinessProbe"
    ]


def test_a_cronjob_and_a_statefulset_and_a_daemonset_are_checked() -> None:
    documents = [
        _workload("CronJob", [_container("tick", probes=())], name="nightly"),
        _workload("StatefulSet", [_container("db")], name="store"),
        _workload("DaemonSet", [_container("agent")], name="node"),
        {"kind": "Service", "metadata": {"name": "web"}, "spec": {}},
    ]

    result = _workload_problems(documents)

    assert result.problems == []
    assert result.checked == {
        ("CronJob/nightly", "tick"),
        ("StatefulSet/store", "db"),
        ("DaemonSet/node", "agent"),
    }


def test_a_cronjob_container_without_resources_is_reported() -> None:
    cronjob = _workload("CronJob", [_container("tick", probes=(), resources={})], name="nightly")

    result = _workload_problems([cronjob])

    assert "CronJob/nightly container tick: resource-missing — resources.requests.cpu not set" in (
        result.problems
    )


def test_every_container_of_a_workload_is_checked() -> None:
    deployment = _workload("Deployment", [_container("first"), _container("second", probes=())])

    result = _workload_problems([deployment])

    assert result.checked == {("Deployment/web", "first"), ("Deployment/web", "second")}
    assert result.problems == [
        "Deployment/web container second: readiness-probe-missing — no readinessProbe"
    ]


def test_a_native_sidecar_init_container_is_held_to_the_main_container_rules() -> None:
    sidecar = _container("proxy", probes=(), resources={}, restart_policy="Always")

    result = _workload_problems(
        [_workload("Deployment", [_container()], init_containers=[sidecar])]
    )

    assert ("Deployment/web", "proxy") in result.checked
    assert result.problems == [
        "Deployment/web container proxy: readiness-probe-missing — no readinessProbe",
        *(
            f"Deployment/web container proxy: resource-missing — resources.{field} not set"
            for field in REQUIRED_RESOURCES
        ),
    ]


def test_no_workload_in_the_render_fails_the_guard() -> None:
    result = _workload_problems([{"kind": "Service", "metadata": {"name": "web"}, "spec": {}}])

    assert result.checked == frozenset()
    assert result.problems == [NO_WORKLOAD]


# --- setup failures: the guard fails, never skips ---


def test_a_missing_helm_fails_the_guard(tmp_path: Path) -> None:
    with pytest.raises(ChartGuardSetupError, match="helm is not on PATH"):
        require_helm(search_path=str(tmp_path))


def test_a_missing_chart_lock_fails_the_guard(tmp_path: Path) -> None:
    chart = _tmp_chart(tmp_path)
    (chart / "Chart.lock").unlink()

    with pytest.raises(ChartGuardSetupError, match=BUILD_HINT_FRAGMENT):
        require_subchart_archives(chart)


def test_a_missing_subchart_archive_fails_the_guard(tmp_path: Path) -> None:
    chart = _tmp_chart(tmp_path)

    with pytest.raises(ChartGuardSetupError, match=BUILD_HINT_FRAGMENT) as raised:
        require_subchart_archives(chart)

    assert f"authentik-{_authentik_lock_version()}.tgz is missing" in str(raised.value)


def test_a_stale_subchart_archive_fails_the_guard(tmp_path: Path) -> None:
    chart = _tmp_chart(tmp_path)
    version = _authentik_lock_version()
    (chart / "charts").mkdir()
    packaged = yaml.safe_dump({"name": "authentik", "version": "0.0.1"}).encode()
    with tarfile.open(chart / "charts" / f"authentik-{version}.tgz", "w:gz") as tar:
        member = tarfile.TarInfo("authentik/Chart.yaml")
        member.size = len(packaged)
        tar.addfile(member, io.BytesIO(packaged))

    with pytest.raises(ChartGuardSetupError, match=BUILD_HINT_FRAGMENT) as raised:
        require_subchart_archives(chart)

    assert f"holds authentik 0.0.1, Chart.lock pins {version}" in str(raised.value)


def test_a_failing_render_fails_the_guard(tmp_path: Path) -> None:
    chart = tmp_path / "refusing"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: refusing\nversion: 0.1.0\n", encoding="utf-8"
    )
    (chart / "templates" / "refuse.yaml").write_text('{{ fail "render refused" }}\n', "utf-8")

    with pytest.raises(ChartGuardSetupError, match=r"helm template exited 1: .*render refused"):
        render(require_helm(), chart, BASE_VALUES)


def test_the_real_chart_archives_are_located() -> None:
    archives = require_subchart_archives(CHART_DIR)

    assert archives == {
        "authentik": CHART_DIR / "charts" / f"authentik-{_authentik_lock_version()}.tgz"
    }


# --- the real chart ---


@pytest.mark.parametrize("profile", list(PROFILES))
def test_the_chart_renders_workloads_that_meet_the_rules(profile: str) -> None:
    result = _workload_problems(_rendered(profile))

    assert result.problems == [], "\n".join(f"{profile}: {p}" for p in result.problems)


def test_the_default_profile_checks_the_chart_workloads_and_its_job() -> None:
    checked = _workload_problems(_rendered("default")).checked

    assert {
        ("Deployment/ps-policy-system-ps-service", "ps-service"),
        ("Deployment/ps-policy-system-falkordb", "falkordb"),
        ("Deployment/ps-policy-system-ps-postgres", "ps-postgres"),
    } <= checked
    assert any(workload.startswith("Job/") for workload, _ in checked)


def test_the_authentik_profile_checks_the_subchart_server_and_worker() -> None:
    result = _workload_problems(_rendered("authentik"))

    assert ("Deployment/ps-authentik-server", "server") in result.checked
    assert ("Deployment/ps-authentik-worker", "worker") in result.checked
    assert result.problems == []
