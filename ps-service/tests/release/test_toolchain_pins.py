"""Static guards on the toolchain versions pinned across CI, the devcontainer and the templates.

Node, the GitHub Actions the workflows depend on, and (further down) Helm are pinned in several
files that must move together: the live devcontainer, the CI composite action, the shipped
`init-takt-devcontainer` templates and the dev-environment inventory. A pin left behind silently
runs a different toolchain locally than in CI, so these tests read every pin from the parsed
file (JSON or YAML, never a substring grep of the raw text) and compare them with one table.

Files are listed with `git ls-files --cached --others --exclude-standard`, so a new file is seen
whether or not it is staged and an ignored file is not.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ".agents/skills/init-takt-devcontainer/templates"
HISTORICAL_PREFIXES = ("docs/audits/", "spikes/", ".orchestrator/")
THIS_FILE = "ps-service/tests/release/test_toolchain_pins.py"

NODE_MAJOR = 26
NODE_FEATURE = "ghcr.io/devcontainers/features/node:1"
DEVCONTAINER_FILES = (
    ".devcontainer/devcontainer.json",
    f"{TEMPLATES}/.devcontainer/devcontainer.json",
)
PREP_RUNNER_FILES = (
    ".github/actions/prep-runner/action.yml",
    f"{TEMPLATES}/.github/actions/prep-runner/action.yml",
)

# action -> major version every `uses:` of it must carry, as an exact `vMAJOR.MINOR.PATCH` tag.
ACTION_MAJORS = {
    "actions/checkout": 7,
    "actions/setup-node": 7,
    "actions/cache": 6,
    "azure/setup-helm": 5,
}
EXACT_TAG = re.compile(r"^v(?P<major>\d+)\.\d+\.\d+$")

HELM_MAJOR = 4
HELM_WORKFLOWS = (
    ".github/workflows/on_semver.yml",
    ".github/workflows/backup-restore-e2e.yml",
)
INSITU_INVENTORY = ".insitu.yml"
PREP_RUNNER_ACTION = ".github/actions/prep-runner/action.yml"
HELM_VERSION = re.compile(r"v(?P<version>\d+\.\d+\.\d+)")
CACHE_KEY_HELM = re.compile(r"-helm(?P<version>\d+\.\d+\.\d+)-")
UNITTEST_PLUGIN_VERSION = "v1.2.1"

# Built from pieces so this file never matches the patterns it forbids.
NODE_24_STEP = re.compile("node-version:" + r"\s*24\b")
NODE_24_FEATURE = re.compile('"version":' + r'\s*"24(\.\d+)*"')


def _visible_files() -> list[str]:
    """Tracked plus untracked-but-not-ignored files, relative to the repository root."""
    listing = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return [path for path in listing.split("\0") if path and (REPO_ROOT / path).is_file()]


def _is_ci_definition(path: str) -> bool:
    """Workflow and composite-action YAML, in this repo and in the shipped templates."""
    in_github = path.startswith((".github/", f"{TEMPLATES}/.github/"))
    return in_github and path.endswith((".yml", ".yaml"))


def _collect_uses(node: object) -> list[str]:
    """Every `uses:` value found anywhere in a parsed YAML document."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in cast("dict[object, object]", node).items():
            if key == "uses" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_collect_uses(value))
    elif isinstance(node, list):
        for item in cast("list[object]", node):
            found.extend(_collect_uses(item))
    return found


def _load_yaml(path: str) -> object:
    return yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))


def _action_refs() -> list[tuple[str, str]]:
    """`(file, uses-value)` for every action reference in CI definitions."""
    return [
        (path, ref)
        for path in _visible_files()
        if _is_ci_definition(path)
        for ref in _collect_uses(_load_yaml(path))
    ]


def _setup_node_versions(path: str) -> list[str]:
    """The `node-version` of each `actions/setup-node` step in a composite action."""
    document = cast("dict[str, object]", _load_yaml(path))
    runs = cast("dict[str, object]", document["runs"])
    steps = cast("list[dict[str, object]]", runs["steps"])
    return [
        str(cast("dict[str, object]", step["with"])["node-version"])
        for step in steps
        if str(step.get("uses", "")).startswith("actions/setup-node@")
    ]


def _devcontainer_node_version(path: str) -> str:
    document = cast("dict[str, object]", json.loads((REPO_ROOT / path).read_text(encoding="utf-8")))
    features = cast("dict[str, dict[str, object]]", document["features"])
    return str(features[NODE_FEATURE]["version"])


def _major(version: str) -> int:
    return int(version.split(".", maxsplit=1)[0])


def test_no_visible_file_pins_node_24() -> None:
    offenders: list[str] = []
    for path in _visible_files():
        if path.startswith(HISTORICAL_PREFIXES) or path == THIS_FILE:
            continue
        text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="ignore")
        if NODE_24_STEP.search(text) or NODE_24_FEATURE.search(text):
            offenders.append(path)
    assert offenders == [], f"files still pinning Node 24: {offenders}"


@pytest.mark.parametrize("path", DEVCONTAINER_FILES)
def test_devcontainer_node_feature_is_the_target_major(path: str) -> None:
    assert _major(_devcontainer_node_version(path)) == NODE_MAJOR


@pytest.mark.parametrize("path", PREP_RUNNER_FILES)
def test_prep_runner_sets_up_the_target_node_major_in_both_variants(path: str) -> None:
    versions = _setup_node_versions(path)
    assert len(versions) == 2, "expected a cached and an uncached setup-node step"
    assert {_major(version) for version in versions} == {NODE_MAJOR}


def test_bumped_actions_use_the_target_major_with_exact_tags() -> None:
    problems: list[str] = []
    seen: set[str] = set()
    for path, ref in _action_refs():
        name, _, tag = ref.partition("@")
        if name not in ACTION_MAJORS:
            continue
        seen.add(name)
        match = EXACT_TAG.match(tag)
        if match is None or int(match["major"]) != ACTION_MAJORS[name]:
            problems.append(f"{path}: {ref} (want exact v{ACTION_MAJORS[name]}.x.y)")
    assert problems == []
    assert seen == set(ACTION_MAJORS), "every bumped action must still be in use somewhere"


def _setup_helm_versions(path: str) -> list[str]:
    """The `version` input of each `azure/setup-helm` step in a workflow."""
    document = cast("dict[str, object]", _load_yaml(path))
    jobs = cast("dict[str, dict[str, object]]", document["jobs"])
    versions: list[str] = []
    for job in jobs.values():
        for step in cast("list[dict[str, object]]", job.get("steps", [])):
            if str(step.get("uses", "")).startswith("azure/setup-helm@"):
                versions.append(str(cast("dict[str, object]", step["with"])["version"]))
    return versions


def _install_helm_command() -> str:
    """The shell body of the `install-helm` entry in the dev-environment inventory."""
    document = cast("dict[str, object]", _load_yaml(INSITU_INVENTORY))
    inventory = cast("list[dict[str, object]]", document["inventory"])
    return next(str(entry["command"]) for entry in inventory if entry["id"] == "install-helm")


def _prep_runner_cache_helm_version() -> str:
    document = cast("dict[str, object]", _load_yaml(PREP_RUNNER_ACTION))
    runs = cast("dict[str, object]", document["runs"])
    for step in cast("list[dict[str, object]]", runs["steps"]):
        key = str(cast("dict[str, object]", step.get("with", {})).get("key", ""))
        match = CACHE_KEY_HELM.search(key)
        if match:
            return match["version"]
    raise AssertionError("no tool-cache key names a Helm version")


def test_helm_is_the_same_version_in_every_pin() -> None:
    installer = _install_helm_command()
    installer_match = re.search(r"DESIRED_VERSION=(?P<version>v\d+\.\d+\.\d+)", installer)
    assert installer_match is not None, "install-helm does not pin DESIRED_VERSION"
    pins = {
        "install-helm": installer_match["version"].removeprefix("v"),
        "tool-cache key": _prep_runner_cache_helm_version(),
    }
    for path in HELM_WORKFLOWS:
        versions = _setup_helm_versions(path)
        assert versions, f"{path} has no setup-helm step"
        for index, version in enumerate(versions):
            match = HELM_VERSION.fullmatch(version)
            assert match is not None, f"{path}: {version} is not an exact version"
            pins[f"{path}#{index}"] = match["version"]
    assert len(set(pins.values())) == 1, f"Helm pins disagree: {pins}"
    assert _major(next(iter(pins.values()))) == HELM_MAJOR


def test_install_helm_uses_the_helm_4_installer() -> None:
    installer = _install_helm_command()
    assert "scripts/get-helm-4" in installer
    assert "get-helm" + "-3" not in installer


def test_install_helm_installs_the_pinned_unverified_unittest_plugin() -> None:
    installer = _install_helm_command()
    install_lines = [line for line in installer.splitlines() if "helm plugin install" in line]
    assert install_lines, "install-helm does not install the unittest plugin"
    for line in install_lines:
        assert "--verify=false" in line, "a git plugin install cannot be signature-verified"
        assert f"--version {UNITTEST_PLUGIN_VERSION}" in line
    skip_check = [line for line in installer.splitlines() if "helm plugin list" in line]
    assert skip_check
    assert all(
        UNITTEST_PLUGIN_VERSION.removeprefix("v").replace(".", r"\.") in line for line in skip_check
    ), "an already-installed plugin of another version must not satisfy the check"


def test_install_helm_does_not_accept_a_helm_of_another_major() -> None:
    assert "helm version" in _install_helm_command()
