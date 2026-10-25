"""Static assertions on `.github/workflows/backup-restore-e2e.yml`.

The workflow runs the backup-restore verifier end to end in a throwaway kind cluster. These
tests pin the properties that keep that safe (it wipes the volumes of the cluster it targets)
and reproducible: triggers and permissions, an owned cluster that is always deleted, exact
version pins, the locally built image, and what a failed run uploads. Same `WorkflowFile`
discipline as `test_pr_to_ready_workflow.py`: parsed structure and `shlex` token vectors,
never substring matching on raw YAML.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from conftest import WorkflowFile

    Step = dict[str, object]

JOB = "e2e"
CLUSTER_NAME = "backup-restore-e2e"
EXACT_ACTION_REF = re.compile(r"@v\d+\.\d+\.\d+$")
TIMEOUT_MINUTES = 15


def _triggers(workflow: WorkflowFile) -> dict[str, object]:
    """The `on:` mapping; PyYAML parses the bare key `on` as the boolean `True`."""
    document = cast("dict[object, object]", workflow.document)
    triggers = document.get("on", document.get(True))
    assert isinstance(triggers, dict), "workflow has no `on:` mapping"
    return cast("dict[str, object]", triggers)


def _run(step: dict[str, object]) -> str:
    return str(step.get("run", ""))


def _all_uses(workflow: WorkflowFile) -> list[str]:
    return [str(step["uses"]) for step in workflow.steps(workflow.job(JOB)) if "uses" in step]


def test_triggers_permissions_and_concurrency_are_locked_down(
    backup_restore_e2e_workflow: WorkflowFile,
) -> None:
    workflow = backup_restore_e2e_workflow
    assert set(_triggers(workflow)) == {"schedule", "workflow_dispatch"}
    assert workflow.mapping(workflow.document, "permissions") == {"contents": "read"}
    assert "secrets." not in workflow.path.read_text(encoding="utf-8")
    concurrency = workflow.mapping(workflow.document, "concurrency")
    assert concurrency.get("group"), "overlapping runs must share one concurrency group"
    assert concurrency.get("cancel-in-progress") is False, "never kill a run mid-wipe"


def test_cluster_is_owned_by_the_job_and_always_deleted(
    backup_restore_e2e_workflow: WorkflowFile,
) -> None:
    workflow = backup_restore_e2e_workflow
    job = workflow.job(JOB)
    creators = workflow.steps_using(job, "helm/kind-action")
    assert len(creators) == 1
    assert workflow.mapping(creators[0], "with").get("cluster_name") == CLUSTER_NAME

    teardown = workflow.steps_running(job, "kind")
    assert len(teardown) == 1
    assert teardown[0].get("if") == "always()"
    assert workflow.tokens(str(teardown[0]["run"])) == [
        "kind",
        "delete",
        "cluster",
        "--name",
        CLUSTER_NAME,
    ]
    assert not any(
        "--allow-context" in workflow.tokens(body) for body in workflow.run_bodies(job)
    ), "the verifier's kind-context guard must stay on"


def test_toolchain_and_every_action_are_pinned_to_exact_versions(
    backup_restore_e2e_workflow: WorkflowFile,
) -> None:
    workflow = backup_restore_e2e_workflow
    job = workflow.job(JOB)
    uses = _all_uses(workflow)
    assert uses, "the job uses no actions"
    for ref in uses:
        assert EXACT_ACTION_REF.search(ref), f"{ref} is not pinned to an exact version"

    kind_with = workflow.mapping(workflow.steps_using(job, "helm/kind-action")[0], "with")
    assert re.fullmatch(r"v\d+\.\d+\.\d+", str(kind_with.get("version")))
    assert re.search(r":v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$", str(kind_with.get("node_image")))
    assert re.search(r"@sha256:[0-9a-f]{64}$", str(kind_with.get("registry_image")))
    helm_with = workflow.mapping(workflow.steps_using(job, "azure/setup-helm")[0], "with")
    assert re.fullmatch(r"v\d+\.\d+\.\d+", str(helm_with.get("version")))


def test_job_has_a_bounded_timeout(backup_restore_e2e_workflow: WorkflowFile) -> None:
    timeout = backup_restore_e2e_workflow.job(JOB).get("timeout-minutes")
    assert isinstance(timeout, int)
    assert timeout == TIMEOUT_MINUTES


REGISTRY_HOST = "kind-registry:5000"
VERIFIER = "scripts/verify-backup-restore.sh"
CHART = "charts/policy-system"


def _index_of(steps: list[dict[str, object]], predicate: Callable[[Step], bool]) -> int:
    matches = [i for i, step in enumerate(steps) if predicate(step)]
    assert len(matches) == 1, f"expected exactly one matching step, got {len(matches)}"
    return matches[0]


def test_install_uses_local_image_and_stub_issuer_profile(
    backup_restore_e2e_workflow: WorkflowFile,
) -> None:
    workflow = backup_restore_e2e_workflow
    job = workflow.job(JOB)
    steps = workflow.steps(job)

    def runs(*needle: str) -> Callable[[Step], bool]:
        return lambda step: all(
            token in workflow.tokens(str(step.get("run", ""))) for token in needle
        )

    def uses(action: str) -> Callable[[Step], bool]:
        return lambda step: str(step.get("uses", "")).startswith(f"{action}@")

    def sets_env(name: str) -> Callable[[Step], bool]:
        return lambda step: name in workflow.mapping(step, "env")

    buildx = _index_of(steps, uses("docker/setup-buildx-action"))
    assert workflow.mapping(steps[buildx], "with").get("driver-opts") == "network=host"
    build = _index_of(steps, uses("docker/build-push-action"))
    build_with = workflow.mapping(steps[build], "with")
    assert build_with.get("push") is True
    assert build_with.get("tags") == f"{REGISTRY_HOST}/ps-service:${{{{ github.sha }}}}"
    repo_add = _index_of(steps, runs("helm", "repo", "add", "authentik"))
    dep_build = _index_of(steps, runs("helm", "dependency", "build", CHART))
    install = _index_of(steps, runs("helm", "install", "policy-system", CHART))
    pods_ready = _index_of(steps, runs("kubectl", "wait", "--for=condition=Ready", "pod"))
    image_check = _index_of(steps, sets_env("EXPECTED"))
    wait_ready = _index_of(steps, runs(VERIFIER, "wait-ready"))
    assert (
        buildx < build < repo_add < dep_build < install < pods_ready < image_check < wait_ready
    ), "build, push, chart deps, install, all-pods gate, image assertion, readiness"

    tokens = workflow.tokens(str(steps[install]["run"]))
    for flag in ("--wait", "--wait-for-jobs"):
        assert flag in tokens
    sets = {t for t in tokens if "=" in t and not t.startswith("--")}
    assert f"psService.image.repository={REGISTRY_HOST}/ps-service" in sets
    assert "psService.image.tag=$GITHUB_SHA" in sets
    assert "authentik.enabled=false" in sets
    assert not any(t.startswith("psService.localTestBypass") for t in sets), (
        "the bypass refuses to start on the container's non-loopback bind"
    )
    assert {s.split("=", 1)[0] for s in sets} >= {
        "psService.auth.issuer",
        "psService.auth.audience",
        "psService.authzBootstrapOwner.subject",
        "psService.authzBootstrapOwner.issuer",
        "psService.publicUrl",
    }
    stub = _index_of(steps, runs("kubectl", "rollout", "status", "deployment/oidc-stub"))
    assert build < stub < install, (
        "the issuer stub runs the built image and must be up before install"
    )
    assert {s.split("=", 1)[0] for s in sets} >= {"llm.azure.apiKey", "llm.azure.apiBase"}
    assert workflow.mapping(steps[image_check], "env")["EXPECTED"] == (
        f"{REGISTRY_HOST}/ps-service:${{{{ github.sha }}}}"
    )
    assert "localhost" not in workflow.path.read_text(encoding="utf-8")


def test_verifier_runs_after_readiness_and_no_step_tolerates_failure(
    backup_restore_e2e_workflow: WorkflowFile,
) -> None:
    workflow = backup_restore_e2e_workflow
    job = workflow.job(JOB)
    assert "continue-on-error" not in job
    steps = workflow.steps(job)
    assert all("continue-on-error" not in step for step in steps)

    retain = [i for i, s in enumerate(steps) if VERIFIER in workflow.tokens(_run(s))]
    names = [workflow.tokens(_run(steps[i])) for i in retain]
    assert [t[t.index(VERIFIER) + 1] for t in names[:2]] == ["wait-ready", "retain-secrets"]
    assert len(names) == 3, "wait-ready, retain-secrets, then one verifier invocation for e2e"
    assert "--chart" in names[1]
    assert names[1][names[1].index("--chart") + 1] == CHART

    e2e_step = steps[retain[2]]
    assert e2e_step.get("id") == "e2e"
    assert e2e_step.get("shell") == "bash", "pipefail must surface a failing verifier behind tee"
    body = workflow.tokens(_run(e2e_step))
    assert "args=(e2e" in body
    assert {"--yes", "--chart", CHART, "--out-dir"} <= set(body)
    assert "--allow-context" not in body
    env = workflow.mapping(job, "env")
    assert {"E2E_OUT", "E2E_LOG"} <= set(env)
    assert re.search(r"e2e_verified .*graphs=\[1-9\]", _run(e2e_step)), (
        "the success line must report at least one graph"
    )


def test_failed_runs_upload_named_diagnostics_and_never_the_backup_tar(
    backup_restore_e2e_workflow: WorkflowFile,
) -> None:
    workflow = backup_restore_e2e_workflow
    job = workflow.job(JOB)
    steps = workflow.steps(job)
    gate = "always() && steps.e2e.outcome != 'success'"

    upload = _index_of(
        steps, lambda s: str(s.get("uses", "")).startswith("actions/upload-artifact@")
    )
    collect = _index_of(steps, lambda s: s.get("name") == "Collect diagnostics")
    e2e = _index_of(steps, lambda s: s.get("id") == "e2e")
    teardown = _index_of(steps, lambda s: workflow.tokens(_run(s))[:2] == ["kind", "delete"])
    assert e2e < collect < upload < teardown
    assert steps[collect].get("if") == gate
    assert steps[upload].get("if") == gate

    upload_with = workflow.mapping(steps[upload], "with")
    assert upload_with.get("name") == "e2e-diagnostics"
    assert upload_with.get("path") == "${{ runner.temp }}/diagnostics"
    assert "tar" not in str(upload_with.get("path")).lower()

    body = _run(steps[collect])
    for artefact in ("digests-before.json", "digests-after.json", "helm status", "kubectl logs"):
        assert artefact in body
    assert not re.search(r"\.tar\b|\$E2E_OUT/\*|\$E2E_OUT\b/?\s*$", body, re.MULTILINE), (
        "only named files leave the out dir; the backup tar holds password hashes"
    )
