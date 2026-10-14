"""Replay the guardrail checks of the CI inventory locally, before pushing.

The CI backstop runs each check's `command` from `.insitu.yml` against a clean checkout. This
script does the same on this machine: it exports the files of the working tree that git tracks
or would track (uncommitted edits included, ignored files excluded) into a temporary directory,
initialises a git repository there, and runs the `command` string of each selected check
verbatim through `sh -c`. It prints one line per check with the id, the exit code and the
seconds, and exits non-zero when any check fails.

Usage: `uv run python scripts/run_ci_guardrails.py [--root PATH] [--ids ID...]`

Without `--ids`, every id in `GUARDRAIL_IDS` that the inventory declares is run. Unknown
explicit ids are an error.

Exit codes: 0 every selected check passed, 1 at least one failed, 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_CLEAN = 0
EXIT_FAILED = 1
EXIT_ERROR = 2

INSITU_FILE = ".insitu.yml"
GUARDRAIL_IDS = (
    "unused-symbols",
    "import-layering",
    "config-drift",
    "doc-links",
    "repo-hygiene",
)
GIT_TIMEOUT_SECONDS = 60.0
CHECK_TIMEOUT_SECONDS = 600.0


class ReplicaError(Exception):
    """The replica could not be set up or an id could not be resolved."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """The result of replaying one check.

    Attributes:
        check_id: Inventory id of the check.
        exit_code: Exit status of the check's command.
        seconds: Wall-clock duration.
    """

    check_id: str
    exit_code: int
    seconds: float

    def render(self) -> str:
        """Format the outcome as one report line.

        Returns:
            The line printed for this check.
        """
        status = "ok" if self.exit_code == 0 else "FAILED"
        return (
            f"run_ci_guardrails.py: {self.check_id}: exit {self.exit_code} "
            f"in {self.seconds:.1f}s ({status})"
        )


def load_commands(root: Path) -> dict[str, str]:
    """Read the command of every inventory check.

    Args:
        root: Repository root holding the inventory file.

    Returns:
        Mapping from check id to its command string.

    Raises:
        ReplicaError: The inventory file is missing or malformed.
    """
    path = root / INSITU_FILE
    if not path.is_file():
        message = f"{INSITU_FILE} not found in {root}"
        raise ReplicaError(message)
    document = cast("dict[str, Any]", yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    inventory = cast("list[dict[str, Any]]", document.get("inventory") or [])
    return {
        str(item["id"]): str(item["command"])
        for item in inventory
        if "id" in item and "command" in item
    }


def select_ids(commands: dict[str, str], requested: Sequence[str]) -> list[str]:
    """Choose the checks to replay.

    Args:
        commands: Inventory commands by id.
        requested: Explicit ids, or empty for the default guardrail set.

    Returns:
        The ids to run, in order.

    Raises:
        ReplicaError: An explicit id is not in the inventory, or nothing is selected.
    """
    if requested:
        unknown = [check_id for check_id in requested if check_id not in commands]
        if unknown:
            message = f"unknown check id(s): {', '.join(unknown)}"
            raise ReplicaError(message)
        return list(requested)
    selected = [check_id for check_id in GUARDRAIL_IDS if check_id in commands]
    if not selected:
        message = "none of the guardrail ids are declared in the inventory"
        raise ReplicaError(message)
    return selected


def _git(root: Path, *args: str) -> str:
    """Run git in a directory and return its standard output.

    Args:
        root: Working directory.
        *args: Git arguments.

    Returns:
        Standard output text.

    Raises:
        ReplicaError: git is missing, times out or fails.
    """
    git = shutil.which("git")
    if git is None:
        message = "git executable not found on PATH"
        raise ReplicaError(message)
    try:
        result = subprocess.run(  # noqa: S603 - absolute git path from shutil.which, list argv, no shell
            [git, *args],
            cwd=root,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        message = f"git {' '.join(args)} timed out"
        raise ReplicaError(message) from error
    if result.returncode != 0:
        message = f"git {' '.join(args)} failed: {result.stderr.strip()}"
        raise ReplicaError(message)
    return result.stdout


def export_tree(root: Path, destination: Path) -> int:
    """Copy tracked and untracked-but-not-ignored files into a fresh git repository.

    Args:
        root: Repository root to export.
        destination: Empty directory to fill.

    Returns:
        The number of files exported.
    """
    listing = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    count = 0
    for name in listing.split("\0"):
        source = root / name
        if not name or not source.is_file():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        count += 1
    _git(destination, "init", "-q", "-b", "main")
    _git(destination, "add", "-A")
    return count


def replay(command: str, workdir: Path, environment: dict[str, str]) -> Outcome:
    """Run one check command verbatim.

    Args:
        command: The inventory command string.
        workdir: Directory holding the exported tree.
        environment: Environment for the command.

    Returns:
        The exit code and duration; a timeout counts as exit 124.
    """
    shell = shutil.which("sh") or "/bin/sh"
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - the command string comes from the repository's own CI inventory
            [shell, "-c", command],
            cwd=workdir,
            env=environment,
            shell=False,
            check=False,
            timeout=CHECK_TIMEOUT_SECONDS,
        )
        code = completed.returncode
    except subprocess.TimeoutExpired:
        code = 124
    return Outcome("", code, time.monotonic() - started)


def main(argv: Sequence[str] | None = None) -> int:
    """Replay the selected checks and print one line per check.

    Args:
        argv: Command-line arguments, defaulting to `sys.argv[1:]`.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    parser.add_argument("--ids", nargs="*", default=[], help="inventory ids to replay")
    args = parser.parse_args(argv)
    root = cast("Path", args.root).resolve()
    try:
        commands = load_commands(root)
        selected = select_ids(commands, cast("list[str]", args.ids))
    except ReplicaError as error:
        sys.stderr.write(f"run_ci_guardrails.py: {error}\n")
        return EXIT_ERROR
    # UV_NO_SYNC keeps `uv run` from re-pointing the shared environment's editable installs at the
    # temporary export.
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "UV_NO_SYNC": "1"}
    if (root / ".venv").is_dir():
        environment["UV_PROJECT_ENVIRONMENT"] = str(root / ".venv")
    failed = False
    with tempfile.TemporaryDirectory(prefix="ci-guardrails-") as scratch:
        workdir = Path(scratch)
        try:
            exported = export_tree(root, workdir)
        except ReplicaError as error:
            sys.stderr.write(f"run_ci_guardrails.py: {error}\n")
            return EXIT_ERROR
        sys.stdout.write(f"run_ci_guardrails.py: exported {exported} files\n")
        for check_id in selected:
            raw = replay(commands[check_id], workdir, environment)
            outcome = Outcome(check_id, raw.exit_code, raw.seconds)
            sys.stdout.write(outcome.render() + "\n")
            failed = failed or outcome.exit_code != 0
    return EXIT_FAILED if failed else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
