"""Static assertions on `.github/workflows/on_semver.yml` — the ps-service release pipeline.

Why this module lives under `ps-service/tests/` rather than a new root-level `tests/`:
the workflow it asserts on is the release pipeline *of ps-service*, the root
`pyproject.toml`'s `testpaths` covers only the two workspace members' test dirs, and
`ps-service/tests` is already inside basedpyright's `include` list — a root-level
`tests/` would be a strict-type-checking blind spot.

`pyyaml` provenance (PLAN D-4): `yaml` is **not** a declared dev dependency. It arrives
as a `litellm` runtime transitive, pinned in the shared `uv.lock`, and is therefore
present in the workspace venv every test run already uses. Declaring it directly is a
legitimate hardening follow-on, deliberately not made part of issue #60 (adding a
dependency is a permission-gated action).

Assertion style (FLAWS F-07b): every assertion is made against the **parsed structure**
(`yaml.safe_load` for the job graph, `needs:` edges, `permissions:` maps and `with:`
mappings) or against `shlex.split` **token vectors** of `run:` bodies, so argument order
and flag/value pairing are asserted rather than mere text presence. No substring
matching against the raw YAML text is used to prove a structural property.

Gate mechanism (FLAWS F-01): the push must be gated on **both** matrix legs
*structurally*. These tests therefore assert the cross-job dependency graph — `needs:`
edges, `permissions:` maps and the absence of a registry login in the build job — and
never step ordering inside a single job. GitHub's implicit `success()` over `needs:`
requires every matrix leg green, which is the property under test.

GitHub Actions cannot be executed locally, so AC-BI-001/AC-BI-005 and the workflow
halves of AC-BI-004/AC-BI-006 are covered here statically only (constraint C-1).
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "on_semver.yml"
_README_PATH = _REPO_ROOT / "README.md"

_GATE_JOB = "verify-tag-on-main"
_BUILD_JOB = "build"
_PUBLISH_JOB = "publish"
_RELEASE_JOBS = (_GATE_JOB, _BUILD_JOB, _PUBLISH_JOB)

_RESOLVE_TAG_STEP_ID = "resolve-tag"
_IMAGE_REFERENCE = "ghcr.io/mindovermachine-dev/ps-service"
_SMOKE_TAG_EXPRESSION = "ps-service:smoke-${{ matrix.arch }}"
_EXPORTED_TARBALL = "/tmp/ps-service-${{ matrix.arch }}.tar"  # noqa: S108 - workflow text, not a path this test opens
_IMAGE_ARTIFACT_EXPRESSION = "image-${{ matrix.arch }}"
_DOWNLOAD_DIR = "/tmp/images"  # noqa: S108 - workflow text, not a path this test opens
_LOADED_TARBALL = "/tmp/images/ps-service-$arch.tar"  # noqa: S108 - workflow text, not a path this test opens
_SMOKE_TAG_IN_SHELL = "ps-service:smoke-$arch"
_BUILD_TAG_IN_SHELL = f"{_IMAGE_REFERENCE}:build-$arch"
_NO_DOCKERFILE_CLAIM = "the repo has no Dockerfile"

_SECRET_REFERENCE = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")
_GITHUB_EXPRESSION = re.compile(r"\$\{\{")
_SHELL_VARIABLE = re.compile(r"\$(\w+)|\$\{(\w+)\}")
_ROUTE_AROUND_THE_GATE = ("always(", "failure(", "cancelled(")

_BASH_TIMEOUT_SECONDS = 10.0


# --------------------------------------------------------------------------------------
# Parsing helpers — every assertion below reads the parsed structure through these.
# --------------------------------------------------------------------------------------


def _load_workflow() -> dict[str, object]:
    """Parse `on_semver.yml` into a mapping.

    Annotating the `yaml.safe_load` result (which is `Any`) at this one boundary keeps
    basedpyright strict clean without a `cast` or a `types-PyYAML` dependency.
    """
    workflow: dict[str, object] = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    return workflow


def _jobs() -> dict[str, dict[str, object]]:
    """Return the workflow's `jobs:` mapping, keyed by job id."""
    jobs = _load_workflow().get("jobs")
    assert isinstance(jobs, dict), "on_semver.yml has no `jobs:` mapping"
    return cast("dict[str, dict[str, object]]", jobs)


def _job(name: str) -> dict[str, object]:
    """Return one job by id, failing with the actual job list when it is absent."""
    jobs = _jobs()
    assert name in jobs, f"job `{name}` is not defined; jobs are {sorted(jobs)}"
    return jobs[name]


def _require_release_graph(jobs: dict[str, dict[str, object]]) -> None:
    """Assert the three-job release graph exists.

    Shared precondition for the invariants that quantify over that graph (referential
    integrity of `needs:`, no gate-routing `if:`, no `${{ }}` inside a `run:` body).
    Without it those tests would quantify over an empty set and pass vacuously.
    """
    missing = [name for name in _RELEASE_JOBS if name not in jobs]
    assert not missing, f"release job(s) {missing} are not defined; jobs are {sorted(jobs)}"


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    """Return a job's `steps:` list."""
    steps = job.get("steps")
    assert isinstance(steps, list), "job has no `steps:` list"
    return cast("list[dict[str, object]]", steps)


def _mapping(step: dict[str, object], key: str) -> dict[str, object]:
    """Return a step's sub-mapping (`with:`, `env:`) as a mapping, or an empty one."""
    value = step.get(key)
    if value is None:
        return {}
    assert isinstance(value, dict), f"step `{key}:` is not a mapping"
    return cast("dict[str, object]", value)


def _needs(job: dict[str, object]) -> list[str]:
    """Return a job's `needs:` as a list, normalising the scalar shorthand."""
    needs = job.get("needs")
    if needs is None:
        return []
    if isinstance(needs, str):
        return [needs]
    assert isinstance(needs, list), "`needs:` is neither a string nor a list"
    return [str(item) for item in cast("list[object]", needs)]


def _tokens(run_body: str) -> list[str]:
    """Tokenise a `run:` body, dropping the newline tokens shlex emits for continuations."""
    return [token for token in shlex.split(run_body) if token != "\n"]


def _run_bodies(job: dict[str, object]) -> list[str]:
    """Return every `run:` body in a job, in declaration order."""
    return [str(step["run"]) for step in _steps(job) if "run" in step]


def _step_with_id(job: dict[str, object], step_id: str) -> dict[str, object] | None:
    """Return the step declaring `id: <step_id>`, or None."""
    return next((step for step in _steps(job) if step.get("id") == step_id), None)


def _steps_using(job: dict[str, object], action: str) -> list[dict[str, object]]:
    """Return every step whose `uses:` names `action` (any pinned version)."""
    return [step for step in _steps(job) if str(step.get("uses", "")).startswith(f"{action}@")]


def _steps_running(job: dict[str, object], token: str) -> list[dict[str, object]]:
    """Return every step whose `run:` body contains `token` as a whole shell token."""
    return [step for step in _steps(job) if token in _tokens(str(step.get("run", "")))]


def _effective_tokens(job: dict[str, object], step: dict[str, object]) -> list[str]:
    """Tokenise a step's `run:` body with its workflow/job/step `env:` bindings substituted in.

    The workflow deliberately routes values through `env:` instead of interpolating `${{ }}`
    into shell text (FLAWS F-11), so `$ARCH` in a body and `ARCH: ${{ matrix.arch }}` in the
    step's `env:` are one fact split across two places. Resolving the chain here lets an
    identity assertion compare a single fully-expanded token vector -- one reason to fail --
    instead of pairing a raw token against a separate lookup. Names with no binding (shell
    loop variables such as `$arch`) are left verbatim.
    """
    bindings: dict[str, str] = {}
    for scope in (_mapping(_load_workflow(), "env"), _mapping(job, "env"), _mapping(step, "env")):
        bindings.update({name: str(value) for name, value in scope.items()})

    def _expand(match: re.Match[str]) -> str:
        return bindings.get(match.group(1) or match.group(2), match.group(0))

    return [_SHELL_VARIABLE.sub(_expand, token) for token in _tokens(str(step.get("run", "")))]


# --------------------------------------------------------------------------------------
# The file parses, the placeholder is gone, the reused gate is intact
# --------------------------------------------------------------------------------------


def test_workflow_yaml_parses_and_every_needs_target_is_a_defined_job() -> None:
    """`on_semver.yml` parses, and every `needs:` edge points at a job that exists.

    Catches the failure mode substring assertions miss entirely: a typo in a `needs:`
    target, which GitHub rejects at workflow load time (FLAWS F-07c).
    """
    jobs = _jobs()
    _require_release_graph(jobs)

    dangling = {
        name: [target for target in _needs(job) if target not in jobs]
        for name, job in jobs.items()
        if any(target not in jobs for target in _needs(job))
    }

    assert not dangling, f"`needs:` targets that are not defined jobs: {dangling}"


def test_placeholder_deploy_to_prod_job_is_gone() -> None:
    """The scaffold's `deploy-to-prod` placeholder job no longer exists."""
    jobs = _jobs()

    assert "deploy-to-prod" not in jobs, "the placeholder `deploy-to-prod` job is still present"

    placeholder_runs = [
        body for job in jobs.values() for body in _run_bodies(job) if "Placeholder deploy" in body
    ]
    assert not placeholder_runs, f"placeholder deploy step body still present: {placeholder_runs}"


def _ancestry_check_step() -> dict[str, object]:
    """Return the gate's single ancestry-check step, failing if it is absent or duplicated."""
    gate = _job(_GATE_JOB)
    steps = [step for step in _steps(gate) if "merge-base" in str(step.get("run", ""))]
    assert len(steps) == 1, f"expected exactly one ancestry check, found {len(steps)}"
    return steps[0]


def test_gate_checks_out_the_full_history_its_ancestry_check_needs() -> None:
    """AC-BI-004: `git merge-base` needs the whole graph, so the gate's checkout is unshallow."""
    checkouts = _steps_using(_job(_GATE_JOB), "actions/checkout")

    assert checkouts, "the gate job no longer checks the repository out"
    assert _mapping(checkouts[0], "with").get("fetch-depth") == 0, (
        "the gate's checkout lost `fetch-depth: 0`; `git merge-base` needs full history"
    )


def test_gate_still_fails_the_run_on_a_tag_that_is_not_an_ancestor_of_main() -> None:
    """AC-BI-004: the existing off-main gate is reused, not weakened or bypassed."""
    tokens = _tokens(str(_ancestry_check_step()["run"]))

    assert "--is-ancestor" in tokens, "the gate no longer runs `git merge-base --is-ancestor`"
    assert "exit" in tokens, "the gate no longer fails the run on an off-main tag"


def test_gate_verifies_the_raw_git_tag_not_the_normalised_image_tag() -> None:
    """FLAWS F-13: the `v`-strip added for AC-BI-001 must not change what the gate verifies."""
    assert _mapping(_ancestry_check_step(), "env").get("TAG_NAME") == (
        f"${{{{ steps.{_RESOLVE_TAG_STEP_ID}.outputs.git-tag }}}}"
    ), "the gate must verify the raw git tag, not the normalised image tag"


# --------------------------------------------------------------------------------------
# The structural push gate (FLAWS F-01) — cross-job, never step order
# --------------------------------------------------------------------------------------


def test_publish_is_the_only_job_that_can_write_to_the_registry() -> None:
    """AC-BI-005/AC-BI-007 and L1 "Security by Design": only `publish` can write to GHCR.

    Structural, not ordinal: the build job declares `contents: read` only and contains no
    registry login, so no build step *can* push regardless of where it sits in the job.

    A job that declares **no** `permissions:` map is counted as write-capable, not as safe.
    It silently inherits the repository's default `GITHUB_TOKEN` scopes, which are configured
    outside this file, can include `packages: write`, and can change without any diff here.
    Merely asking `"packages" in permissions` would pass such a job vacuously, which is
    exactly the hole this test exists to close.
    """
    jobs = _jobs()
    _require_release_graph(jobs)

    assert _job(_PUBLISH_JOB).get("permissions") == {"contents": "read", "packages": "write"}
    assert _job(_BUILD_JOB).get("permissions") == {"contents": "read"}

    write_capable = [
        name
        for name, job in jobs.items()
        if name != _PUBLISH_JOB
        and (
            not isinstance(job.get("permissions"), dict)
            or "packages" in _mapping(job, "permissions")
        )
    ]
    assert not write_capable, (
        f"jobs other than `{_PUBLISH_JOB}` hold, or inherit by declaring no `permissions:` "
        f"map at all, a token that may carry `packages: write`: {write_capable}"
    )

    logins = [
        name
        for name, job in jobs.items()
        if name != _PUBLISH_JOB and _steps_using(job, "docker/login-action")
    ]
    assert not logins, f"jobs other than `{_PUBLISH_JOB}` log in to a registry: {logins}"


def test_publish_job_needs_both_the_gate_and_the_build_job() -> None:
    """AC-BI-007: the push cannot run unless the gate and every build leg succeeded.

    GitHub's implicit `success()` over `needs:` requires **all** legs of the `build` matrix
    green. This is the structural replacement for the deleted step-ordering test (F-01).
    """
    _require_release_graph(_jobs())
    needs = _needs(_job(_PUBLISH_JOB))

    assert set(needs) == {_GATE_JOB, _BUILD_JOB}, (
        f"`{_PUBLISH_JOB}` needs {sorted(needs)}, not both `{_GATE_JOB}` and `{_BUILD_JOB}`"
    )


def test_publish_job_declares_no_job_level_if_condition() -> None:
    """AC-BI-007: a job-level `if:` replaces the implicit `success()` over `needs:`."""
    _require_release_graph(_jobs())

    assert "if" not in _job(_PUBLISH_JOB), (
        f"a job-level `if:` on `{_PUBLISH_JOB}` replaces the implicit `success()` over `needs:`"
    )


def test_build_job_itself_depends_on_the_gate() -> None:
    """AC-BI-004: nothing is built for a tag the off-main gate has not cleared."""
    _require_release_graph(_jobs())

    assert _needs(_job(_BUILD_JOB)) == [_GATE_JOB], "`build` must itself depend on the gate"


def test_build_is_the_matrix_job_so_that_needing_it_needs_every_leg() -> None:
    """AC-BI-007: the smoke tests live in the matrix job `publish` declares in its `needs:`."""
    assert "matrix" in _mapping(_job(_BUILD_JOB), "strategy"), (
        "`build` must be the matrix job, so that needing it needs every leg"
    )


def test_build_matrix_does_not_fail_fast() -> None:
    """Both legs' smoke results are wanted for diagnostics; the gate is `needs:`, not fail-fast."""
    assert _mapping(_job(_BUILD_JOB), "strategy").get("fail-fast") is False, (
        "`fail-fast: false` keeps both legs' smoke results; the gate is `needs:`, not fail-fast"
    )


def test_no_build_step_pushes_to_a_registry() -> None:
    """The build job builds and loads locally; it performs zero registry writes."""
    build = _job(_BUILD_JOB)

    build_steps = _steps_using(build, "docker/build-push-action")
    assert len(build_steps) == 1, f"expected exactly one build step, found {len(build_steps)}"

    options = _mapping(build_steps[0], "with")
    assert options.get("push") is False, "the build step must not push"
    assert options.get("load") is True, "the build step must load the image for the smoke test"
    assert options.get("tags") == _SMOKE_TAG_EXPRESSION

    pushing = [body for body in _run_bodies(build) if "push" in _tokens(body)]
    assert not pushing, f"a `run:` body in the build job pushes: {pushing}"


def test_matrix_covers_exactly_amd64_and_arm64_on_native_runners() -> None:
    """AC-BI-006: one native runner per architecture, no emulation."""
    matrix = _mapping(_mapping(_job(_BUILD_JOB), "strategy"), "matrix")
    legs = matrix.get("include")

    assert legs == [
        {"runner": "ubuntu-latest", "platform": "linux/amd64", "arch": "amd64"},
        {"runner": "ubuntu-24.04-arm", "platform": "linux/arm64", "arch": "arm64"},
    ], f"matrix legs are {legs!r}"
    assert _job(_BUILD_JOB).get("runs-on") == "${{ matrix.runner }}"


# --------------------------------------------------------------------------------------
# The published tags (AC-BI-001) and the single tag derivation (F-13)
# --------------------------------------------------------------------------------------


def test_publish_creates_a_manifest_list_tagged_with_the_release_tag_and_latest() -> None:
    """AC-BI-001/AC-BI-006: one `imagetools create` merges both legs into two tags.

    Asserted as a token vector, so flag/value pairing and argument order are proven, not
    just the presence of the strings somewhere in the body.
    """
    merge_bodies = [body for body in _run_bodies(_job(_PUBLISH_JOB)) if "imagetools" in body]
    assert len(merge_bodies) >= 1, "no `docker buildx imagetools` step in the publish job"

    create = [body for body in merge_bodies if "create" in _tokens(body)]
    assert len(create) == 1, f"expected exactly one `imagetools create`, found {len(create)}"

    assert _tokens(create[0]) == [
        "docker",
        "buildx",
        "imagetools",
        "create",
        "-t",
        "$IMAGE:$IMAGE_TAG",
        "-t",
        "$IMAGE:latest",
        "$IMAGE:build-amd64",
        "$IMAGE:build-arm64",
    ]
    assert _load_workflow().get("env") == {"IMAGE": _IMAGE_REFERENCE}


def test_image_tag_is_derived_from_the_gated_tag_by_one_expression() -> None:
    """AC-BI-001/F-13: exactly one place normalises the tag, and publish consumes it.

    Two derivations would let the verified tag and the published tag drift apart.
    """
    assert _job(_GATE_JOB).get("outputs") == {
        "git-tag": f"${{{{ steps.{_RESOLVE_TAG_STEP_ID}.outputs.git-tag }}}}",
        "image-tag": f"${{{{ steps.{_RESOLVE_TAG_STEP_ID}.outputs.image-tag }}}}",
    }
    assert _mapping(_job(_PUBLISH_JOB), "env").get("IMAGE_TAG") == (
        f"${{{{ needs.{_GATE_JOB}.outputs.image-tag }}}}"
    )

    text = _WORKFLOW_PATH.read_text(encoding="utf-8")
    assert text.count("${TAG_NAME#v}") == 1, (
        "the leading-`v` strip must appear exactly once in the whole workflow"
    )


def test_release_tag_is_normalised_by_stripping_a_leading_v(tmp_path: Path) -> None:
    """AC-BI-001: the resolve-tag snippet is **executed**, not just read.

    `v1.2.3` and `1.2.3` must publish the identical `:1.2.3`; a pre-release prefix such as
    `rc1.2.3` must survive untouched (FLAWS F-07d).
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this machine")

    step = _step_with_id(_job(_GATE_JOB), _RESOLVE_TAG_STEP_ID)
    assert step is not None, f"the gate job has no step with `id: {_RESOLVE_TAG_STEP_ID}`"
    snippet = str(step["run"])

    for tag_name, expected_image_tag in (
        ("v1.2.3", "1.2.3"),
        ("1.2.3", "1.2.3"),
        ("rc1.2.3", "rc1.2.3"),
    ):
        output_file = tmp_path / f"{tag_name}.env"
        output_file.touch()
        subprocess.run(  # noqa: S603 - bash is a shutil.which-resolved absolute path, the snippet is read from the repo's own workflow (see docstring)
            [bash, "-c", snippet],
            check=True,
            capture_output=True,
            text=True,
            timeout=_BASH_TIMEOUT_SECONDS,
            env={"TAG_NAME": tag_name, "GITHUB_OUTPUT": str(output_file), "PATH": "/usr/bin:/bin"},
        )

        assert output_file.read_text(encoding="utf-8").splitlines() == [
            f"git-tag={tag_name}",
            f"image-tag={expected_image_tag}",
        ]


# --------------------------------------------------------------------------------------
# Credentials (AC-BI-005) and the smoke gate's subject (AC-BI-007)
# --------------------------------------------------------------------------------------


def test_only_secret_referenced_by_the_publishing_job_is_github_token() -> None:
    """AC-BI-005: no long-lived PAT anywhere in the job that writes to GHCR."""
    publish = yaml.safe_dump(_job(_PUBLISH_JOB))
    referenced = set(_SECRET_REFERENCE.findall(publish))

    assert referenced == {"GITHUB_TOKEN"}, f"secrets referenced by `{_PUBLISH_JOB}`: {referenced}"


def test_ghcr_login_uses_the_workflow_token() -> None:
    """AC-BI-005: the GHCR login is the scoped `GITHUB_TOKEN`, against `ghcr.io`."""
    logins = _steps_using(_job(_PUBLISH_JOB), "docker/login-action")
    assert len(logins) == 1, f"expected exactly one GHCR login step, found {len(logins)}"

    credentials = _mapping(logins[0], "with")
    assert credentials.get("registry") == "ghcr.io"
    assert credentials.get("password") == "${{ secrets.GITHUB_TOKEN }}"
    assert credentials.get("username") == "${{ github.actor }}"


def test_smoke_test_step_runs_the_container_image_module_against_the_built_tag() -> None:
    """AC-BI-007: the smoke test runs against the tag the build step just produced.

    This is the first link of the identity chain (FLAWS F-02); the remaining links —
    export, upload, download, push — are asserted by the four tests in the section below.
    """
    build = _job(_BUILD_JOB)
    smoke = [
        step for step in _steps(build) if "test_container_image.py" in str(step.get("run", ""))
    ]
    assert len(smoke) == 1, f"expected exactly one smoke-test step, found {len(smoke)}"

    assert _tokens(str(smoke[0]["run"])) == [
        "uv",
        "run",
        "pytest",
        "ps-service/tests/test_container_image.py",
        "-m",
        "container_image",
        "-q",
    ]
    assert _mapping(smoke[0], "env").get("PS_CONTAINER_IMAGE_REF") == _SMOKE_TAG_EXPRESSION, (
        "the smoke test must target the tag the build step produced"
    )


# --------------------------------------------------------------------------------------
# The image-identity chain (FLAWS F-02): the thing we tested is the thing we ship.
#
# `_SMOKE_TAG_EXPRESSION` is the one image identity. The build step produces it, the smoke
# step tests it, and the four tests below pin every remaining hop by which those exact bytes
# reach GHCR: exported to a tarball, uploaded as the per-arch artifact, downloaded by
# `publish`, loaded and pushed **without a second build**. Each hop is asserted separately so
# a break names the hop that broke. Without them the claim would be a docstring only: a
# publish job that rebuilt the image, or dropped the export entirely, would still ship.
# --------------------------------------------------------------------------------------


def test_build_job_exports_the_smoke_tested_image_to_a_tarball() -> None:
    """AC-BI-007: the bytes that passed the smoke test are captured, not re-derived later.

    `docker image save` must name the *same* tag the build step produced and the smoke step
    tested, so the fully-expanded vector is compared against `_SMOKE_TAG_EXPRESSION` itself.
    """
    build = _job(_BUILD_JOB)
    exports = _steps_running(build, "save")
    assert len(exports) == 1, f"expected exactly one image export, found {len(exports)}"

    assert _effective_tokens(build, exports[0]) == [
        "docker",
        "image",
        "save",
        _SMOKE_TAG_EXPRESSION,
        "-o",
        _EXPORTED_TARBALL,
    ], "the export must save the smoke-tested tag itself to the tarball `publish` downloads"


def test_build_job_uploads_the_exported_tarball_as_the_per_arch_artifact() -> None:
    """AC-BI-007: the exported tarball is the artifact `publish` later downloads by name.

    Dropping this step (or renaming the artifact) severs the chain: `publish` would have no
    smoke-tested bytes to load, and only a rebuild could still produce something to push.
    """
    uploads = _steps_using(_job(_BUILD_JOB), "actions/upload-artifact")
    assert len(uploads) == 1, f"expected exactly one artifact upload, found {len(uploads)}"

    options = _mapping(uploads[0], "with")
    assert (options.get("name"), options.get("path")) == (
        _IMAGE_ARTIFACT_EXPRESSION,
        _EXPORTED_TARBALL,
    ), f"the upload must carry {_EXPORTED_TARBALL} as {_IMAGE_ARTIFACT_EXPRESSION}: {options}"


def test_publish_job_downloads_both_uploaded_image_artifacts() -> None:
    """AC-BI-006/AC-BI-007: `publish` starts from both legs' smoke-tested tarballs."""
    downloads = _steps_using(_job(_PUBLISH_JOB), "actions/download-artifact")

    assert [
        (_mapping(step, "with").get("name"), _mapping(step, "with").get("path"))
        for step in downloads
    ] == [
        ("image-amd64", _DOWNLOAD_DIR),
        ("image-arm64", _DOWNLOAD_DIR),
    ], "`publish` must download exactly the two per-arch image artifacts the build job uploaded"


def test_publish_job_pushes_the_image_it_loaded_from_the_smoke_tested_tarball() -> None:
    """AC-BI-002/AC-BI-007: what is pushed is what was smoke-tested, byte for byte.

    One fully-expanded token vector pins the whole hop: load the downloaded tarball, tag
    **that loaded image** — `ps-service:smoke-$arch`, the tag the smoke test ran against — as
    the per-arch build tag, and push that. Tagging any other local reference (a base image, a
    freshly built one) or sourcing the tarball from anywhere else breaks this vector.
    """
    publish = _job(_PUBLISH_JOB)
    pushes = _steps_running(publish, "push")
    assert len(pushes) == 1, f"expected exactly one registry-push step, found {len(pushes)}"

    assert _effective_tokens(publish, pushes[0]) == [
        "for",
        "arch",
        "in",
        "amd64",
        "arm64;",
        "do",
        "docker",
        "image",
        "load",
        "-i",
        _LOADED_TARBALL,
        "docker",
        "image",
        "tag",
        _SMOKE_TAG_IN_SHELL,
        _BUILD_TAG_IN_SHELL,
        "docker",
        "image",
        "push",
        _BUILD_TAG_IN_SHELL,
        "done",
    ], "the pushed image must be the loaded, smoke-tested one and nothing else"


def test_publish_job_never_rebuilds_the_image() -> None:
    """AC-BI-007: no second image identity is created in the job that holds the push token.

    A `docker build` — or a `build-push-action` — inside `publish` would ship bytes no smoke
    test ever ran against, however green both build legs were. `docker buildx imagetools`
    is not a build: it merges manifests that already exist in the registry.
    """
    publish = _job(_PUBLISH_JOB)

    assert not _steps_using(publish, "docker/build-push-action"), (
        f"`{_PUBLISH_JOB}` builds an image instead of shipping the smoke-tested one"
    )
    rebuilding = [body for body in _run_bodies(publish) if "build" in _tokens(body)]
    assert not rebuilding, f"a `run:` body in `{_PUBLISH_JOB}` rebuilds the image: {rebuilding}"


# --------------------------------------------------------------------------------------
# Invariants over the whole release graph, and the README claim
# --------------------------------------------------------------------------------------


def test_no_job_runs_under_an_always_or_failure_condition() -> None:
    """AC-BI-004/AC-BI-007: nothing routes around the gate.

    `if: always()` / `failure()` / `!cancelled()` on a job or a step would run it even
    after the gate or a smoke test failed, defeating the `needs:` graph.
    """
    jobs = _jobs()
    _require_release_graph(jobs)

    conditions = [
        (name, str(condition))
        for name, job in jobs.items()
        for condition in [job.get("if"), *[step.get("if") for step in _steps(job)]]
        if condition is not None
        and any(marker in str(condition) for marker in _ROUTE_AROUND_THE_GATE)
    ]

    assert not conditions, f"conditions that route around the gate: {conditions}"


def test_no_run_body_interpolates_a_github_expression() -> None:
    """FLAWS F-11: `${{ }}` never reaches a shell body; values arrive through `env:`.

    Direct interpolation splices an attacker-controllable string into the script text
    before the shell parses it.
    """
    jobs = _jobs()
    _require_release_graph(jobs)

    interpolating = [
        (name, body)
        for name, job in jobs.items()
        for body in _run_bodies(job)
        if _GITHUB_EXPRESSION.search(body)
    ]

    assert not interpolating, f"`run:` bodies interpolating a GitHub expression: {interpolating}"


def test_readme_no_longer_claims_the_repo_has_no_dockerfile() -> None:
    """The README's "no Dockerfile" disclaimer is retired once the image is published."""
    readme = _README_PATH.read_text(encoding="utf-8")

    assert _NO_DOCKERFILE_CLAIM not in readme, f"README.md still claims {_NO_DOCKERFILE_CLAIM!r}"
