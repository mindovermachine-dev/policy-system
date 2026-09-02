"""Static contract tests for the repository-root `Dockerfile` and `.dockerignore` (issue #60).

No repo linter covers either file — `prettier`, `cspell` and `markdownlint-cli2` all have
no handler for them, and `ruff`/`basedpyright` only see Python. These tests are therefore
the *only* automated gate on the container build inputs, which is why they assert the
build-critical properties (stage count, `uv sync` flags, interpreter pin, ignore shape)
rather than a formatting style.

Placement: this module mirrors no source module — it asserts facts about repository-root
artefacts. It lives at the `ps-service/tests/` root (alongside the equally non-mirroring
`test_main_integration.py`) because `testpaths` is `["ps-service/tests", "ps-cli/tests"]`
and basedpyright's `include` covers `ps-service/tests`; a new root-level `tests/` directory
would require changing both and would still leave a type-checking gap.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import pytest

from ps_service.config import ServiceConfigurationError, load_config

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "Dockerfile"
_DOCKERIGNORE = _REPO_ROOT / ".dockerignore"
_PS_SERVICE_MANIFEST = _REPO_ROOT / "ps-service" / "pyproject.toml"

_MINIMUM_BUILD_STAGES = 2
_VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)")

# The five build inputs `.dockerignore` must re-include after its deny-all line, per the
# uv workspace layout: the workspace root manifest + lock, the member being synced, its
# source tree, and the sibling member's manifest (uv loads every declared workspace member).
_REQUIRED_REINCLUDES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "ps-service/pyproject.toml",
        "ps-service/src",
        "ps-cli/pyproject.toml",
    }
)

# Belt-and-braces denials that must survive any re-inclusion of a parent tree.
_REQUIRED_HARD_DENIES = frozenset(
    {
        "**/__pycache__",
        "**/*.py[codz]",
        "**/.venv",
        "**/tests",
    }
)


def _read(path: Path, description: str) -> str:
    """Read a repository-root build input, failing with a legible message when it is absent."""
    assert path.is_file(), f"expected {description} at {path}"
    return path.read_text(encoding="utf-8")


def _instructions(dockerfile: str) -> list[tuple[str, str]]:
    """Return `(KEYWORD, argument)` pairs, with line continuations joined and comments dropped."""
    logical: list[str] = []
    pending = ""
    for raw_line in dockerfile.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        logical.append(pending + line)
        pending = ""
    if pending:
        logical.append(pending.strip())

    pairs: list[tuple[str, str]] = []
    for statement in logical:
        keyword, _, argument = statement.partition(" ")
        pairs.append((keyword.upper(), argument.strip()))
    return pairs


def _environment(dockerfile: str) -> dict[str, str]:
    """Return every `ENV` key/value pair declared anywhere in the Dockerfile."""
    env: dict[str, str] = {}
    for keyword, argument in _instructions(dockerfile):
        if keyword != "ENV":
            continue
        for token in shlex.split(argument):
            key, separator, value = token.partition("=")
            if separator:
                env[key] = value
    return env


def _base_images(dockerfile: str) -> list[str]:
    """Return the image reference of every `FROM` stage, in file order."""
    return [
        shlex.split(argument)[0]
        for keyword, argument in _instructions(dockerfile)
        if keyword == "FROM"
    ]


def _minor_version(text: str) -> tuple[int, int]:
    """Parse the first `<major>.<minor>` out of a version string, e.g. a tag or a specifier."""
    match = _VERSION_PATTERN.search(text)
    assert match is not None, f"no <major>.<minor> version found in {text!r}"
    return (int(match.group(1)), int(match.group(2)))


def test_dockerfile_declares_at_least_two_build_stages() -> None:
    """A multi-stage build is what keeps uv and the build scratch out of the runtime layer.

    AC-BI-003: a single-stage image would ship the builder's toolchain.
    """
    stages = _base_images(_read(_DOCKERFILE, "a Dockerfile"))

    assert len(stages) >= _MINIMUM_BUILD_STAGES, f"expected a multi-stage build, got {stages}"


def test_uv_sync_invocations_pass_package_frozen_and_no_dev() -> None:
    """Every `uv sync` must name the member, honour the lock verbatim, and exclude dev deps.

    `--package ps-service` scopes the sync to the member being imaged, `--frozen` makes a
    stale `uv.lock` fail the build instead of silently re-resolving, and `--no-dev` is the
    mechanism behind AC-BI-003 (no pytest/ruff/basedpyright in the runtime image).
    """
    syncs = [
        shlex.split(argument)
        for keyword, argument in _instructions(_read(_DOCKERFILE, "a Dockerfile"))
        if keyword == "RUN" and "uv sync" in argument
    ]

    assert syncs, "expected at least one `RUN uv sync ...` instruction"
    for tokens in syncs:
        assert "--package" in tokens, f"missing --package in {tokens}"
        assert tokens[tokens.index("--package") + 1] == "ps-service", f"wrong --package in {tokens}"
        assert "--frozen" in tokens, f"missing --frozen in {tokens}"
        assert "--no-dev" in tokens, f"missing --no-dev in {tokens}"


def test_dockerfile_disables_uv_python_downloads() -> None:
    """`UV_PYTHON_DOWNLOADS=never` makes the base image's interpreter the only interpreter.

    Without it uv silently downloads its own interpreter when the base image's Python does
    not satisfy `requires-python`, which would make the `FROM` pin a lie and defeat
    `test_dockerfile_base_image_python_version_matches_ps_service_requires_python`. With it,
    a mismatch fails the build loudly (L1 "Fail Fast at Boundaries").
    """
    environment = _environment(_read(_DOCKERFILE, "a Dockerfile"))

    assert environment.get("UV_PYTHON_DOWNLOADS") == "never"


def test_dockerfile_base_image_python_version_matches_ps_service_requires_python() -> None:
    """The image's interpreter minor version must equal `ps-service`'s `requires-python` floor.

    The Dockerfile is a new declaration site for the Python version and nothing else
    compares it against the package it installs; a drifted base image would either fail
    obscurely at `uv sync` or (worse) ship an interpreter the code was never checked against.
    """
    manifest = tomllib.loads(_read(_PS_SERVICE_MANIFEST, "ps-service/pyproject.toml"))
    requires_python = manifest["project"]["requires-python"]
    assert isinstance(requires_python, str)
    expected = _minor_version(requires_python)

    python_stages = [
        ref for ref in _base_images(_read(_DOCKERFILE, "a Dockerfile")) if ref.startswith("python:")
    ]

    assert python_stages, "expected every build stage to be based on a `python:` image"
    for reference in python_stages:
        _, _, tag = reference.partition(":")
        assert _minor_version(tag) == expected, (
            f"{reference} does not match requires-python {requires_python!r}"
        )


def test_source_default_host_is_still_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container's wider bind must come from the Dockerfile only, never from the source.

    D-6: `ps-service` binds loopback-only and unauthenticated until issue #39 ships a
    network-reachable, authenticated transport. `ENV PS_SERVICE_HOST=0.0.0.0` belongs in the
    image, where the wider bind is deliberate; widening the source default would expose an
    unauthenticated service on every developer's machine and would silently invert the
    non-loopback startup warning. Host resolution correspondingly fails closed — an invalid
    `PS_SERVICE_HOST` raises rather than substituting the wider `0.0.0.0` fallback.
    """
    monkeypatch.delenv("PS_SERVICE_HOST", raising=False)
    assert load_config().host == "127.0.0.1"

    monkeypatch.setenv("PS_SERVICE_HOST", "   ")
    with pytest.raises(ServiceConfigurationError):
        load_config()


def test_dockerignore_denies_all_then_reincludes_only_the_build_inputs() -> None:
    """`.dockerignore` must be an allow-list, not a deny-list, and keep the heavy trees out.

    A deny-list goes stale silently as the repo grows; an allow-list can only ever be wrong
    in the loud direction (a missing entry fails the build instead of fattening the image).
    Asserted as properties — deny-all first, the five build inputs re-included, the four
    hard denies present — so a later BuildKit-driven addition does not require rewriting it.
    """
    lines = [
        line.strip()
        for line in _read(_DOCKERIGNORE, "a .dockerignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert lines, "expected a non-empty .dockerignore"
    assert lines[0] == "*", (
        f"expected the first non-comment line to deny everything, got {lines[0]!r}"
    )

    reincluded = {line.removeprefix("!") for line in lines if line.startswith("!")}
    assert reincluded >= _REQUIRED_REINCLUDES, (
        f"missing re-includes: {sorted(_REQUIRED_REINCLUDES - reincluded)}"
    )

    denies = {line for line in lines if not line.startswith("!")}
    assert denies >= _REQUIRED_HARD_DENIES, (
        f"missing hard denies: {sorted(_REQUIRED_HARD_DENIES - denies)}"
    )
