"""Flag repository hygiene problems in the files tracked by git.

Five rules, each reported with its rule id:

- `tracked-backup-file`: a tracked editor or merge leftover (`.bak`, `.orig`, `.rej`, `.swp`,
  `.swo`, or a name ending in `~`).
- `exec-without-shebang`: a tracked file with the executable bit whose first line is not a
  shebang, so the bit does nothing useful.
- `tracked-but-ignored`: a tracked file that an ignore rule also matches, so the ignore rule is
  not doing what it says.
- `duplicate-gitignore-rule`: the same rule listed twice in one `.gitignore`.
- `readme-layout-missing-row`: a top-level entry of the repository that has no row in a table
  of `README.md`. Skipped when the root has no `README.md`.

The scan reads git metadata and file prefixes only. It never executes a scanned file.

Usage: `uv run python scripts/check_repo_hygiene.py [--root PATH]`

Exit codes: 0 clean, 1 at least one finding, 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

GIT_TIMEOUT_SECONDS = 30.0
BACKUP_SUFFIXES = (".bak", ".orig", ".rej", ".swp", ".swo", "~")
EXECUTABLE_MODE = "100755"
SHEBANG = b"#!"
README = "README.md"
_ROW_PATTERN = re.compile(r"^\|\s*`([^`]+)`", re.MULTILINE)
_GUIDE = "See CONTRIBUTING.md#automated-guardrails."


class EnvironmentFailureError(Exception):
    """The check could not run: git is missing or failed."""


@dataclass(frozen=True, slots=True)
class Finding:
    """A single repository hygiene finding.

    Attributes:
        rule: Rule id printed with the finding.
        file: Repository-relative path the finding points at.
        line: 1-based line number, or 1 when the finding concerns the whole file.
        message: What is wrong.
        fix: One-line instruction for resolving it.
    """

    rule: str
    file: str
    line: int
    message: str
    fix: str

    def render(self) -> str:
        """Format the finding as one report line.

        Returns:
            The line printed for this finding.
        """
        return (
            f"check_repo_hygiene.py: RULE {self.rule}: {self.file}:{self.line}: "
            f"{self.message}. How to fix: {self.fix}. {_GUIDE}"
        )


def _git(root: Path, *args: str) -> str:
    """Run a read-only git command in the root.

    Args:
        root: Repository root.
        *args: Arguments after `git`.

    Returns:
        The standard output of the command.

    Raises:
        EnvironmentFailureError: git is missing, times out or fails.
    """
    git = shutil.which("git")
    if git is None:
        message = "git executable not found on PATH"
        raise EnvironmentFailureError(message)
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
        message = f"git {args[0]} timed out after {GIT_TIMEOUT_SECONDS}s"
        raise EnvironmentFailureError(message) from error
    if result.returncode != 0:
        message = f"git {args[0]} failed: {result.stderr.strip()}"
        raise EnvironmentFailureError(message)
    return result.stdout


def _tracked_entries(root: Path) -> list[tuple[str, str]]:
    """List tracked files with their git file mode.

    Args:
        root: Repository root.

    Returns:
        Pairs of (mode, repository-relative POSIX path).
    """
    entries: list[tuple[str, str]] = []
    for record in _git(root, "ls-files", "-s", "-z").split("\0"):
        if not record:
            continue
        meta, _, name = record.partition("\t")
        entries.append((meta.split(" ", 1)[0], name))
    return entries


def _backup_findings(names: Sequence[str]) -> list[Finding]:
    return [
        Finding(
            "tracked-backup-file",
            name,
            1,
            "editor or merge leftover is tracked",
            "delete the file with `git rm`",
        )
        for name in names
        if name.endswith(BACKUP_SUFFIXES)
    ]


def _shebang_findings(root: Path, entries: Sequence[tuple[str, str]]) -> list[Finding]:
    findings: list[Finding] = []
    for mode, name in entries:
        if mode != EXECUTABLE_MODE:
            continue
        path = root / name
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            head = handle.read(len(SHEBANG))
        if head != SHEBANG:
            findings.append(
                Finding(
                    "exec-without-shebang",
                    name,
                    1,
                    "file is executable but does not start with a shebang",
                    "add a shebang line or run `git update-index --chmod=-x` on the file",
                )
            )
    return findings


def _ignored_findings(root: Path) -> list[Finding]:
    listing = _git(root, "ls-files", "-ci", "--exclude-standard", "-z")
    return [
        Finding(
            "tracked-but-ignored",
            name,
            1,
            "file is tracked but an ignore rule matches it",
            "untrack it with `git rm --cached` or fix the ignore rule",
        )
        for name in listing.split("\0")
        if name
    ]


def _duplicate_rule_findings(root: Path, names: Sequence[str]) -> list[Finding]:
    findings: list[Finding] = []
    for name in names:
        if PurePosixPath(name).name != ".gitignore":
            continue
        path = root / name
        if not path.is_file():
            continue
        seen: dict[str, int] = {}
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            rule = raw.strip()
            if not rule or rule.startswith("#"):
                continue
            if rule in seen:
                findings.append(
                    Finding(
                        "duplicate-gitignore-rule",
                        name,
                        number,
                        f"rule `{rule}` repeats line {seen[rule]}",
                        "delete the repeated line",
                    )
                )
            else:
                seen[rule] = number
    return findings


def _readme_findings(root: Path, names: Sequence[str]) -> list[Finding]:
    readme = root / README
    if not readme.is_file():
        return []
    documented = {
        match.group(1).rstrip("/") for match in _ROW_PATTERN.finditer(readme.read_text("utf-8"))
    }
    top_level = sorted({PurePosixPath(name).parts[0] for name in names})
    return [
        Finding(
            "readme-layout-missing-row",
            README,
            1,
            f"top-level entry `{entry}` has no row in the layout table",
            f"add a row for `{entry}` to the repository layout table",
        )
        for entry in top_level
        if entry not in documented
    ]


def scan(root: Path) -> list[Finding]:
    """Run every hygiene rule over the tracked files of a repository.

    Args:
        root: Repository root.

    Returns:
        All findings, in rule order.
    """
    entries = _tracked_entries(root)
    names = [name for _, name in entries]
    return [
        *_backup_findings(names),
        *_shebang_findings(root, entries),
        *_ignored_findings(root),
        *_duplicate_rule_findings(root, names),
        *_readme_findings(root, names),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check and print one line per finding.

    Args:
        argv: Command-line arguments, defaulting to `sys.argv[1:]`.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    args = parser.parse_args(argv)
    root = cast("Path", args.root).resolve()
    try:
        findings = scan(root)
    except EnvironmentFailureError as error:
        sys.stderr.write(f"check_repo_hygiene.py: {error}\n")
        return EXIT_ERROR
    for finding in findings:
        sys.stdout.write(finding.render() + "\n")
    return EXIT_FINDINGS if findings else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
