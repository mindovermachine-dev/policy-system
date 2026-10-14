"""Flag broken links, anchors and repository paths in markdown documents.

Three rules, each reported with its rule id:

- `doc-link-missing`: a relative markdown link whose target file or directory does not exist.
- `doc-link-anchor`: a link to `#fragment` in a markdown file that has no such heading.
- `doc-path-missing`: a backticked path rooted at a known top-level repository directory that
  does not exist. Paths containing `*` or `<` are patterns and are skipped.

Fenced code blocks are ignored. Reports, generated regulation text, templates with
placeholders, skill assets and test data are out of scope (see `EXCLUDED_PREFIXES`).

The scan reads files as text. It never fetches URLs: absolute links are not checked.

Usage: `uv run python scripts/check_doc_links.py [--root PATH] [--tracked | FILE...]`

Modes:
    FILE...: the listed markdown files; files outside the scope or missing on disk are skipped.
    --tracked: every markdown file tracked by git in the root.

Exit codes: 0 clean, 1 at least one finding, 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

GIT_TIMEOUT_SECONDS = 30.0
EXCLUDED_PREFIXES = (
    "docs/audits/",
    "docs/regulations/",
    "docs/templates/",
    "test-data/",
    "node_modules/",
    ".venv/",
    ".orchestrator/",
    "spikes/",
)
SKILL_ASSET_MARKER = "/assets/"
PATH_ROOTS = (
    "docs|ps-service|ps-cli|ps-skills|charts|scripts|tools|deploy|curated-content"
    "|internal-sources|ps-test-support|ps-test-plugin|\\.github"
)

_LINK_PATTERN = re.compile(r"\]\(([^)\s]+)")
_ABSOLUTE_PATTERN = re.compile(r"(?:https?|mailto):")
_PATH_PATTERN = re.compile(rf"`((?:{PATH_ROOTS})/[\w./\-*<>]+)`")
_HEADING_PATTERN = re.compile(r"#{1,6}\s+(.*)")
_GUIDE = "See CONTRIBUTING.md#automated-guardrails."


class EnvironmentFailureError(Exception):
    """The check could not run: git is missing or failed."""


@dataclass(frozen=True, slots=True)
class Finding:
    """A single documentation reference problem.

    Attributes:
        rule: Rule id printed with the finding.
        file: Repository-relative path of the document.
        line: 1-based line number of the reference.
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
            f"check_doc_links.py: RULE {self.rule}: {self.file}:{self.line}: "
            f"{self.message}. How to fix: {self.fix}. {_GUIDE}"
        )


def in_scope(relative: str) -> bool:
    """Tell whether a markdown file is checked.

    Args:
        relative: Repository-relative POSIX path.

    Returns:
        True when the file is markdown outside every excluded location.
    """
    if not relative.endswith(".md") or relative.startswith(EXCLUDED_PREFIXES):
        return False
    return not (relative.startswith(".claude/") and SKILL_ASSET_MARKER in relative)


def slugify(heading: str) -> str:
    """Convert a heading to its GitHub anchor slug.

    Args:
        heading: Heading text without the leading hashes.

    Returns:
        The anchor slug.
    """
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def heading_anchors(text: str) -> set[str]:
    """Collect the anchors of every heading outside fenced code blocks.

    Args:
        text: Markdown source.

    Returns:
        The anchors, with `-1`, `-2` suffixes for repeated headings.
    """
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        match = _HEADING_PATTERN.match(line)
        if match and not fenced:
            slug = slugify(match.group(1))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def _read(path: Path) -> str:
    """Read a file as text, replacing undecodable bytes.

    Args:
        path: File to read.

    Returns:
        The file text.
    """
    return path.read_text(encoding="utf-8", errors="replace")


def _check_link(document: Path, relative: str, number: int, target: str) -> Finding | None:
    """Check one relative link target.

    Args:
        document: Absolute path of the document holding the link.
        relative: Repository-relative path of the document.
        number: 1-based line number of the link.
        target: Link target as written, including any fragment.

    Returns:
        A finding when the file or heading is missing, otherwise None.
    """
    if _ABSOLUTE_PATTERN.match(target):
        return None
    path_part, _, fragment = target.partition("#")
    destination = document.parent / path_part if path_part else document
    if path_part and not destination.exists():
        return Finding(
            "doc-link-missing",
            relative,
            number,
            f"link target {target} does not exist",
            "fix the relative path or remove the link",
        )
    if fragment and destination.suffix == ".md" and destination.is_file():
        anchors = heading_anchors(_read(destination))
        if slugify(fragment) not in anchors and fragment not in anchors:
            return Finding(
                "doc-link-anchor",
                relative,
                number,
                f"link {target} points at a heading that does not exist",
                "match the anchor to an existing heading",
            )
    return None


def _check_paths(root: Path, relative: str, number: int, line: str) -> list[Finding]:
    """Check the backticked repository paths of one line.

    Args:
        root: Repository root.
        relative: Repository-relative path of the document.
        number: 1-based line number.
        line: The line text.

    Returns:
        One finding per referenced path that does not exist.
    """
    findings: list[Finding] = []
    for match in _PATH_PATTERN.finditer(line):
        candidate = match.group(1).rstrip("/.")
        if "*" in candidate or "<" in candidate or (root / candidate).exists():
            continue
        findings.append(
            Finding(
                "doc-path-missing",
                relative,
                number,
                f"referenced path {candidate} does not exist",
                "correct the path or remove the reference",
            )
        )
    return findings


def scan_document(root: Path, relative: str) -> list[Finding]:
    """Check every link, anchor and path reference of one document.

    Args:
        root: Repository root.
        relative: Repository-relative path of the document.

    Returns:
        The findings, in line order.
    """
    document = root / relative
    findings: list[Finding] = []
    fenced = False
    for number, line in enumerate(_read(document).splitlines(), start=1):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced:
            for match in _LINK_PATTERN.finditer(line):
                finding = _check_link(document, relative, number, match.group(1))
                if finding is not None:
                    findings.append(finding)
            findings.extend(_check_paths(root, relative, number, line))
    return findings


def tracked_markdown(root: Path) -> list[str]:
    """List the markdown files tracked by git.

    Args:
        root: Repository root.

    Returns:
        Repository-relative POSIX paths.

    Raises:
        EnvironmentFailureError: git is missing, times out or fails.
    """
    git = shutil.which("git")
    if git is None:
        message = "git executable not found on PATH"
        raise EnvironmentFailureError(message)
    try:
        result = subprocess.run(  # noqa: S603 - absolute git path from shutil.which, list argv, no shell
            [git, "ls-files", "-z", "--", "*.md"],
            cwd=root,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        message = f"git ls-files timed out after {GIT_TIMEOUT_SECONDS}s"
        raise EnvironmentFailureError(message) from error
    if result.returncode != 0:
        message = f"git ls-files failed: {result.stderr.strip()}"
        raise EnvironmentFailureError(message)
    return [name for name in result.stdout.split("\0") if name]


def _explicit_documents(root: Path, names: Sequence[str]) -> list[str]:
    """Normalise listed files to root-relative paths that exist.

    Args:
        root: Repository root.
        names: Paths as given on the command line, absolute or relative to the root.

    Returns:
        Root-relative POSIX paths of the files that exist inside the root.
    """
    relatives: list[str] = []
    for name in names:
        path = Path(name)
        absolute = path if path.is_absolute() else root / path
        if not absolute.is_file():
            continue
        try:
            relatives.append(absolute.resolve().relative_to(root).as_posix())
        except ValueError:
            continue
    return relatives


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check and print one line per finding.

    Args:
        argv: Command-line arguments, defaulting to `sys.argv[1:]`.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    parser.add_argument("--tracked", action="store_true", help="check every tracked markdown file")
    parser.add_argument("files", nargs="*", help="markdown files to check")
    args = parser.parse_args(argv)
    root = cast("Path", args.root).resolve()
    tracked = cast("bool", args.tracked)
    files = cast("list[str]", args.files)
    if tracked == bool(files):
        sys.stderr.write("check_doc_links.py: pass either --tracked or at least one file\n")
        return EXIT_ERROR
    try:
        candidates = tracked_markdown(root) if tracked else _explicit_documents(root, files)
    except EnvironmentFailureError as error:
        sys.stderr.write(f"check_doc_links.py: {error}\n")
        return EXIT_ERROR
    findings = [
        finding
        for relative in candidates
        if in_scope(relative)
        for finding in scan_document(root, relative)
    ]
    for finding in findings:
        sys.stdout.write(finding.render() + "\n")
    return EXIT_FINDINGS if findings else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
