"""Static assertions on `.github/dependabot.yml` and the workflows Dependabot pull requests meet.

Dependabot only proposes pull requests; the config must stay parseable, point at manifests that
exist, produce commit headers the release scripts accept, and leave the uv pins that Dependabot
cannot see in step. No workflow may merge on its own, so only a human approval reaches
`pr-to-ready`. Same discipline as the sibling workflow tests: parsed structure and `shlex`
token vectors, never substring matching on raw YAML (comments aside).
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING, cast

import pytest
import yaml

if TYPE_CHECKING:
    from pathlib import Path

CONFIG_PATH = ".github/dependabot.yml"
ECOSYSTEMS = {"uv", "npm", "docker", "helm", "github-actions"}
RUNTIME_PREFIX_ECOSYSTEMS = {"uv", "docker", "helm"}
ACTIONS_DIRECTORIES = ["/", "/.github/actions/prep-runner"]
UV_IMAGE = "ghcr.io/astral-sh/uv"
SAMPLE_DESCRIPTION = "bump example from 1.0.0 to 1.0.1"
SHA = "a" * 40
SEMVER = r"\d+\.\d+\.\d+"
UV_PIN_FILES = (
    "Dockerfile",
    ".github/actions/prep-runner/action.yml",
    ".github/workflows/backup-restore-e2e.yml",
    ".github/workflows/on_semver.yml",
    ".devcontainer/devcontainer.json",
)
UV_PIN_COMMENT = re.compile(rf"^# .*?({SEMVER}) is the version pinned in CI")
UV_IMAGE_PIN = re.compile(rf"astral-sh/uv:({SEMVER})")
# Manifests and locks each ecosystem targets, relative to its directory.
TARGETS = {
    "uv": ["pyproject.toml", "uv.lock"],
    "npm": ["package.json", "package-lock.json"],
    "docker": ["Dockerfile"],
    "helm": ["Chart.yaml", "Chart.lock"],
}
ACTIONS_TARGETS = {
    "/": ".github/workflows/on_dev.yml",
    "/.github/actions/prep-runner": ".github/actions/prep-runner/action.yml",
}


def _load(repo_root: Path) -> dict[str, object]:
    document = yaml.safe_load((repo_root / CONFIG_PATH).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast("dict[str, object]", document)


def _updates(repo_root: Path) -> list[dict[str, object]]:
    updates = _load(repo_root).get("updates")
    assert isinstance(updates, list)
    return cast("list[dict[str, object]]", updates)


def _by_ecosystem(repo_root: Path) -> dict[str, dict[str, object]]:
    return {str(update["package-ecosystem"]): update for update in _updates(repo_root)}


def _directories(update: dict[str, object]) -> list[str]:
    if "directories" in update:
        return [str(item) for item in cast("list[object]", update["directories"])]
    return [str(update["directory"])]


def _mapping(node: dict[str, object], key: str) -> dict[str, object]:
    value = node.get(key)
    assert isinstance(value, dict), f"`{key}:` is not a mapping"
    return cast("dict[str, object]", value)


def _tracked(repo_root: Path) -> set[str]:
    listing = subprocess.run(
        ["git", "ls-files", "-z"],  # noqa: S607 - git is a devcontainer and CI prerequisite
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return {path for path in listing.stdout.split("\0") if path}


def test_declares_version_2_and_exactly_the_five_ecosystems_weekly(repo_root: Path) -> None:
    assert _load(repo_root)["version"] == 2
    updates = _updates(repo_root)
    assert {str(update["package-ecosystem"]) for update in updates} == ECOSYSTEMS
    assert len(updates) == len(ECOSYSTEMS), "one entry per ecosystem"
    for update in updates:
        assert _mapping(update, "schedule")["interval"] == "weekly", update["package-ecosystem"]


def test_every_directory_exists_and_its_manifests_are_tracked(repo_root: Path) -> None:
    tracked = _tracked(repo_root)
    updates = _by_ecosystem(repo_root)
    for ecosystem, files in TARGETS.items():
        (directory,) = _directories(updates[ecosystem])
        assert (repo_root / directory.lstrip("/")).is_dir(), f"{ecosystem}: {directory}"
        for name in files:
            path = f"{directory.strip('/')}/{name}".lstrip("/")
            assert path in tracked, f"{ecosystem}: {path} is not tracked"


def test_github_actions_lists_the_root_and_the_composite_action_directory(
    repo_root: Path,
) -> None:
    update = _by_ecosystem(repo_root)["github-actions"]
    directories = _directories(update)
    assert sorted(directories) == sorted(ACTIONS_DIRECTORIES)
    tracked = _tracked(repo_root)
    for directory in directories:
        assert (repo_root / directory.lstrip("/")).is_dir(), directory
        assert ACTIONS_TARGETS[directory] in tracked, directory


def _prefixes(update: dict[str, object]) -> list[str]:
    message = _mapping(update, "commit-message")
    return [str(message[key]) for key in ("prefix", "prefix-development") if key in message]


def test_every_prefix_yields_a_header_the_lint_accepts(repo_root: Path) -> None:
    lint = repo_root / "scripts" / "release" / "lint-commit-header.sh"
    for ecosystem, update in _by_ecosystem(repo_root).items():
        prefixes = _prefixes(update)
        assert prefixes, f"{ecosystem} has no commit-message prefix"
        for prefix in prefixes:
            header = f"{prefix}: {SAMPLE_DESCRIPTION}"
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["bash", str(lint), "--header", header],  # noqa: S607
                cwd=repo_root,
                check=False,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"{ecosystem}: `{header}` rejected: {result.stderr}"


def test_runtime_ecosystems_release_a_patch(repo_root: Path) -> None:
    bump = repo_root / "scripts" / "release" / "bump-from-commits.sh"
    updates = _by_ecosystem(repo_root)
    for ecosystem in RUNTIME_PREFIX_ECOSYSTEMS:
        prefix = str(_mapping(updates[ecosystem], "commit-message")["prefix"])
        record = f"{SHA}\n{prefix}: {SAMPLE_DESCRIPTION}\n\n\0"
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["bash", str(bump)],  # noqa: S607
            cwd=repo_root,
            input=record,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "bump=patch", f"{ecosystem}: {result.stdout!r}"


def test_non_runtime_ecosystems_do_not_release(repo_root: Path) -> None:
    bump = repo_root / "scripts" / "release" / "bump-from-commits.sh"
    updates = _by_ecosystem(repo_root)
    for ecosystem in ("npm", "github-actions"):
        prefix = str(_mapping(updates[ecosystem], "commit-message")["prefix"])
        record = f"{SHA}\n{prefix}: {SAMPLE_DESCRIPTION}\n\n\0"
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["bash", str(bump)],  # noqa: S607
            cwd=repo_root,
            input=record,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "bump=none", f"{ecosystem}: {result.stdout!r}"


def test_no_group_includes_major_updates(repo_root: Path) -> None:
    for update in _updates(repo_root):
        groups = update.get("groups")
        if groups is None:
            continue
        assert isinstance(groups, dict)
        for name, group in cast("dict[str, dict[str, object]]", groups).items():
            update_types = group.get("update-types")
            assert isinstance(update_types, list), f"group {name} must list update-types"
            assert "major" not in update_types, f"group {name} groups major updates"
            assert update_types, f"group {name} lists no update types"


def test_docker_ignores_the_uv_image_and_python_major_and_minor(repo_root: Path) -> None:
    ignore = _by_ecosystem(repo_root)["docker"]["ignore"]
    assert isinstance(ignore, list)
    entries = {
        str(item["dependency-name"]): item for item in cast("list[dict[str, object]]", ignore)
    }
    assert UV_IMAGE in entries
    assert "update-types" not in entries[UV_IMAGE], "the uv image is excluded entirely"
    python_types = entries["python"]["update-types"]
    assert isinstance(python_types, list)
    ignored = {str(item) for item in cast("list[object]", python_types)}
    assert {"version-update:semver-major", "version-update:semver-minor"} <= ignored


def _dockerfile_pins(repo_root: Path) -> list[str]:
    lines = (repo_root / "Dockerfile").read_text(encoding="utf-8").splitlines()
    pins: list[str] = []
    for line in lines:
        image = UV_IMAGE_PIN.search(line)
        if image:
            pins.append(image.group(1))
    return pins


def _dockerfile_comments(repo_root: Path) -> list[str]:
    lines = (repo_root / "Dockerfile").read_text(encoding="utf-8").splitlines()
    return [m.group(1) for line in lines if (m := UV_PIN_COMMENT.match(line))]


def _setup_uv_versions(repo_root: Path, relative: str) -> list[str]:
    text = (repo_root / relative).read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            mapping = cast("dict[str, object]", node)
            uses = mapping.get("uses")
            if isinstance(uses, str) and uses.startswith("astral-sh/setup-uv@"):
                version = cast("dict[str, object]", mapping.get("with") or {}).get("version")
                if version is not None:
                    found.append(str(version))
            for child in mapping.values():
                walk(child)
        elif isinstance(node, list):
            for child in cast("list[object]", node):
                walk(child)

    walk(document)
    return found


def _devcontainer_pins(repo_root: Path) -> list[str]:
    text = (repo_root / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
    document = cast("dict[str, object]", json.loads(text))
    features = _mapping(document, "features")
    return [
        str(_mapping(features, name)["version"])
        for name in features
        if re.search(r"/uv:\d+$", name)
    ]


def test_uv_pins_are_the_six_known_ones_plus_one_comment_and_agree(repo_root: Path) -> None:
    pins = {
        "Dockerfile": _dockerfile_pins(repo_root),
        ".github/actions/prep-runner/action.yml": _setup_uv_versions(
            repo_root, ".github/actions/prep-runner/action.yml"
        ),
        ".github/workflows/backup-restore-e2e.yml": _setup_uv_versions(
            repo_root, ".github/workflows/backup-restore-e2e.yml"
        ),
        ".github/workflows/on_semver.yml": _setup_uv_versions(
            repo_root, ".github/workflows/on_semver.yml"
        ),
        ".devcontainer/devcontainer.json": _devcontainer_pins(repo_root),
    }
    assert {name: len(found) for name, found in pins.items()} == {
        "Dockerfile": 1,
        ".github/actions/prep-runner/action.yml": 1,
        ".github/workflows/backup-restore-e2e.yml": 1,
        ".github/workflows/on_semver.yml": 2,
        ".devcontainer/devcontainer.json": 1,
    }
    comments = _dockerfile_comments(repo_root)
    assert len(comments) == 1, "expected one prose comment naming the uv version"
    versions = {version for found in pins.values() for version in found} | set(comments)
    assert len(versions) == 1, f"uv pins disagree: {pins} comment {comments}"
    assert all(re.fullmatch(SEMVER, version) for version in versions)


def test_no_other_tracked_file_pins_uv(repo_root: Path) -> None:
    """A uv version pinned outside the inventory would drift unseen."""
    known = set(UV_PIN_FILES)
    stray: list[str] = []
    for path in sorted(_tracked(repo_root)):
        if path in known or path.startswith(".agents/"):
            continue
        is_actions_file = path.startswith(".github/") and path.endswith((".yml", ".yaml"))
        pins_setup_uv = is_actions_file and bool(_setup_uv_versions(repo_root, path))
        pins_image = path.endswith("Dockerfile") and "astral-sh/uv:" in _read(repo_root, path)
        if pins_setup_uv or pins_image:
            stray.append(path)
    assert not stray, f"uv pins outside the inventory: {stray}"


def _read(repo_root: Path, relative: str) -> str:
    return (repo_root / relative).read_text(encoding="utf-8", errors="ignore")


def test_config_comment_lists_every_uv_pin_location(repo_root: Path) -> None:
    text = (repo_root / CONFIG_PATH).read_text(encoding="utf-8")
    comment = "\n".join(line for line in text.splitlines() if line.lstrip().startswith("#"))
    for relative in UV_PIN_FILES:
        assert relative in comment, f"{relative} is missing from the config comment"


def _workflow_steps(repo_root: Path) -> list[tuple[str, dict[str, object]]]:
    files = [
        *sorted((repo_root / ".github" / "workflows").glob("*.yml")),
        *sorted((repo_root / ".github" / "actions").rglob("action.yml")),
    ]
    collected: list[tuple[str, dict[str, object]]] = []
    for file in files:
        document = yaml.safe_load(file.read_text(encoding="utf-8"))
        assert isinstance(document, dict)
        root = cast("dict[str, object]", document)
        jobs = root.get("jobs")
        runs = root.get("runs")
        step_lists: list[object] = []
        if isinstance(jobs, dict):
            step_lists = [
                job.get("steps") for job in cast("dict[str, dict[str, object]]", jobs).values()
            ]
        elif isinstance(runs, dict):
            step_lists = [cast("dict[str, object]", runs).get("steps")]
        for steps in step_lists:
            if isinstance(steps, list):
                collected.extend(
                    (file.name, step) for step in cast("list[dict[str, object]]", steps)
                )
    return collected


def test_no_workflow_merges_automatically(repo_root: Path) -> None:
    steps = _workflow_steps(repo_root)
    assert steps, "no workflow steps found"
    offenders: list[str] = []
    for name, step in steps:
        uses = str(step.get("uses", ""))
        if uses.startswith("dependabot/fetch-metadata"):
            offenders.append(f"{name}: {uses}")
        run = step.get("run")
        if not isinstance(run, str):
            continue
        tokens = [token for token in re.split(r"\s+", run) if token]
        for index, token in enumerate(tokens):
            if token == "--auto":
                offenders.append(f"{name}: --auto")
            if token == "gh" and tokens[index + 1 : index + 3] == ["pr", "merge"]:
                offenders.append(f"{name}: gh pr merge")
    assert not offenders, f"auto-merge machinery found: {offenders}"


@pytest.mark.parametrize("ecosystem", sorted(ECOSYSTEMS))
def test_each_entry_sets_only_known_keys(repo_root: Path, ecosystem: str) -> None:
    allowed = {
        "package-ecosystem",
        "directory",
        "directories",
        "schedule",
        "commit-message",
        "groups",
        "ignore",
    }
    update = _by_ecosystem(repo_root)[ecosystem]
    assert set(update) <= allowed, sorted(set(update) - allowed)
    assert ("directory" in update) != ("directories" in update), "exactly one directory form"
