"""Flag provenance text in the docstrings and comments of Python files.

Provenance text points a reader at material they cannot open from the code alone and goes stale
as that material changes: acceptance criterion ids, issue and pull-request numbers, and
references to planning artefacts such as plan, handoff or tracker documents. Code text should
state what the code does and why, nothing else.

The scan reads files as text and parses them with `ast` and `tokenize`. It never imports,
compiles or executes a scanned file. Only docstrings (including bare string statements) and
comments are inspected; ordinary string literals, external URLs and identifiers that merely
contain a banned word are ignored.

Usage: `uv run python scripts/check_docstring_provenance.py [paths...] [--changed-since REF]
[--tracked]`

Modes:
    paths: explicit files or directories (recursive); every finding is reported.
    --changed-since REF: files that differ from the merge base of REF and HEAD, including
        staged, unstaged and untracked files; only findings on added lines are reported.
    --tracked: every file tracked by git; every finding is reported.

Exit codes: 0 clean, 1 at least one finding, 2 usage or environment error (including an
unparsable file or an unresolvable git reference).
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import shutil
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

GIT_TIMEOUT_SECONDS = 30.0
EXCLUDED_DIRS = frozenset({"spikes", ".venv", "node_modules", ".orchestrator"})
PYTHON_SUFFIX = ".py"

_URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HUNK_PATTERN = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ProvenancePattern:
    """One named provenance regular expression."""

    pattern_id: str
    regex: re.Pattern[str]


def _pattern(pattern_id: str, expression: str, flags: int = 0) -> ProvenancePattern:
    """Build a named pattern.

    Args:
        pattern_id: Short identifier printed with every finding.
        expression: Regular expression source.
        flags: `re` flags applied when compiling.

    Returns:
        The compiled named pattern.
    """
    return ProvenancePattern(pattern_id, re.compile(expression, flags))


PATTERNS: tuple[ProvenancePattern, ...] = (
    _pattern("ac-id", r"\bAC-(?:[A-Z]{2,}-)?\d+\b"),
    _pattern("issue-hash", r"(?<![\w&/])#\d+[A-Za-z]?\b"),
    _pattern(
        "issue-word",
        r"\b(?:issue|PR|pull request|GH|ticket)[\s_-]*#?\d+\b",
        re.IGNORECASE,
    ),
    _pattern("planning-doc", r"\b(?:PLAN|FLAWS|CHANGES|HANDOFF|BASELINE)(?:\.md)?\b"),
    _pattern(
        "planning-ref",
        r"\b(?:plan|hand-?off)\s+(?:doc(?:ument)?|file|note)s?\b",
        re.IGNORECASE,
    ),
    _pattern("spike-ref", r"\bspikes?/\S*", re.IGNORECASE),
    _pattern(
        "spike-ref",
        r"\bspike\s+(?:doc(?:ument)?|report|readme|finding|result)s?\b",
        re.IGNORECASE,
    ),
    _pattern("spike-ref", r"\bspike-\d+\b", re.IGNORECASE),
    _pattern("orchestrator-ref", r"(?<![\w)\]])\.orchestrator\b"),
    _pattern("orchestrator-ref", r"\btracker/i\d+"),
    _pattern(
        "gitignored-ref",
        r"\bgit-?ignored\s+(?:plan|doc|spike|tracker)\w*",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True, slots=True)
class Finding:
    """A single provenance match in a docstring or comment."""

    file: str
    line: int
    pattern_id: str
    text: str

    def format(self) -> str:
        """Render the finding as `<file>:<line>: <pattern>: <matched text>`.

        Returns:
            The single output line for this finding.
        """
        return f"{self.file}:{self.line}: {self.pattern_id}: {self.text}"


@dataclass(frozen=True, slots=True)
class ScanError:
    """A file that could not be read or parsed."""

    file: str
    line: int
    message: str

    def format(self) -> str:
        """Render the error as `<file>:<line>: error: <message>`.

        Returns:
            The single output line for this error.
        """
        return f"{self.file}:{self.line}: error: {self.message}"


@dataclass(frozen=True, slots=True)
class Target:
    """A file to scan and the lines on which findings count.

    `added_lines` is `None` when every finding is reported.
    """

    path: Path
    display: str
    added_lines: frozenset[int] | None


class GitError(Exception):
    """Raised when a git invocation fails, times out or cannot start."""


class ScanFailure(Exception):  # noqa: N818 - a failure to scan, not an error condition of the program
    """Raised when one file cannot be decoded or parsed."""

    def __init__(self, line: int, message: str) -> None:
        """Record where and why scanning failed.

        Args:
            line: One-based line of the failure, or 1 when unknown.
            message: Human-readable reason.
        """
        super().__init__(message)
        self.line = line
        self.message = message


# --- scanning ---------------------------------------------------------------------------------


def _mask_urls(text: str) -> str:
    """Replace every external URL with spaces of equal length.

    Args:
        text: Docstring or comment text.

    Returns:
        The text with URLs blanked so offsets and line numbers are unchanged.
    """
    return _URL_PATTERN.sub(lambda match: " " * len(match.group()), text)


def _find_in_text(text: str, relative_path: str, first_line: int) -> list[Finding]:
    """Match every provenance pattern against text.

    Args:
        text: Raw docstring or comment text.
        relative_path: Path recorded on each finding.
        first_line: Line number of the first character of `text`.

    Returns:
        One finding per match, with the line derived from the match offset.
    """
    masked = _mask_urls(text)
    findings: list[Finding] = []
    for pattern in PATTERNS:
        for match in pattern.regex.finditer(masked):
            line = first_line + masked.count("\n", 0, match.start())
            findings.append(Finding(relative_path, line, pattern.pattern_id, match.group().strip()))
    return findings


def _char_column(lines: list[str], row: int, byte_column: int) -> int:
    """Convert an `ast` UTF-8 byte offset into a character offset.

    Args:
        lines: Source split on newline characters.
        row: One-based row.
        byte_column: Zero-based UTF-8 byte offset within the row.

    Returns:
        The zero-based character offset matching the tokenizer's columns.
    """
    line = lines[row - 1] if 1 <= row <= len(lines) else ""
    return len(line.encode("utf-8")[:byte_column].decode("utf-8", errors="ignore"))


def _docstring_spans(
    tree: ast.Module, lines: list[str]
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Locate every docstring-like statement as a character-coordinate span.

    Args:
        tree: Parsed module.
        lines: Source split on newline characters.

    Returns:
        `((start_row, start_col), (end_row, end_col))` for each string-only expression
        statement, which covers module, class and function docstrings and bare string
        statements.
    """
    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        value = node.value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        if value.end_lineno is None or value.end_col_offset is None:
            continue
        start = (value.lineno, _char_column(lines, value.lineno, value.col_offset))
        end = (value.end_lineno, _char_column(lines, value.end_lineno, value.end_col_offset))
        spans.append((start, end))
    return spans


def _in_any_span(
    position: tuple[int, int], spans: Iterable[tuple[tuple[int, int], tuple[int, int]]]
) -> bool:
    """Report whether a token start lies inside one of the spans.

    Args:
        position: `(row, col)` of the token start.
        spans: Candidate spans.

    Returns:
        True when `start <= position < end` for some span.
    """
    return any(start <= position < end for start, end in spans)


def scan_source(source: str, relative_path: str) -> list[Finding]:
    """Scan one module's text for provenance in docstrings and comments.

    The text is parsed with `ast` and `tokenize` only; nothing is imported or executed.

    Args:
        source: Python source text.
        relative_path: Path recorded on each finding.

    Returns:
        Findings sorted by line, pattern and text, without duplicates.

    Raises:
        SyntaxError: If the source does not parse.
        tokenize.TokenError: If the source cannot be tokenized.
    """
    tree = ast.parse(source, filename=relative_path)
    spans = _docstring_spans(tree, source.split("\n"))
    findings: set[Finding] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            findings.update(_find_in_text(token.string[1:], relative_path, token.start[0]))
        elif token.type == tokenize.STRING and _in_any_span(token.start, spans):
            findings.update(_find_in_text(token.string, relative_path, token.start[0]))
    return sorted(findings, key=lambda f: (f.line, f.pattern_id, f.text))


def parse_added_lines(diff_text: str) -> frozenset[int]:
    """Collect the new-side line numbers added by a zero-context unified diff.

    Args:
        diff_text: Output of `git diff -U0` for one file.

    Returns:
        The set of added or changed line numbers; deletion-only hunks contribute none.
    """
    added: set[int] = set()
    for match in _HUNK_PATTERN.finditer(diff_text):
        start = int(match.group(1))
        count = 1 if match.group(2) is None else int(match.group(2))
        added.update(range(start, start + count))
    return frozenset(added)


def _read_source(path: Path) -> str:
    """Read a Python file honouring its declared encoding.

    Args:
        path: File to read.

    Returns:
        The decoded source text.

    Raises:
        ScanFailure: If the file cannot be read or decoded.
    """
    try:
        data = path.read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data.decode(encoding)
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError) as exc:
        raise ScanFailure(1, f"cannot read file: {exc}") from exc


def _scan_target(target: Target) -> list[Finding]:
    """Scan one target and apply its added-line filter.

    Args:
        target: File to scan.

    Returns:
        The findings that count for this target.

    Raises:
        ScanFailure: If the file cannot be read, decoded or parsed.
    """
    source = _read_source(target.path)
    try:
        findings = scan_source(source, target.display)
    except SyntaxError as exc:
        raise ScanFailure(exc.lineno or 1, f"cannot parse file: {exc.msg}") from exc
    except (tokenize.TokenError, ValueError) as exc:
        raise ScanFailure(1, f"cannot tokenize file: {exc}") from exc
    if target.added_lines is None:
        return findings
    return [f for f in findings if f.line in target.added_lines]


# --- target selection -------------------------------------------------------------------------


def _is_excluded(parts: Iterable[str]) -> bool:
    """Report whether any path component is an excluded directory name.

    Args:
        parts: Components of a path relative to the scan root.

    Returns:
        True when the path lies under an excluded directory.
    """
    return any(part in EXCLUDED_DIRS for part in parts)


def run_git(args: Sequence[str], cwd: Path, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    """Run git with a fixed argument list and return its standard output.

    Args:
        args: Arguments after `git`.
        cwd: Directory to run in.
        timeout: Seconds before the call is abandoned.

    Returns:
        Standard output text.

    Raises:
        GitError: If git is missing, times out, or exits non-zero.
    """
    git = shutil.which("git")
    if git is None:
        message = "git executable not found on PATH"
        raise GitError(message)
    try:
        result = subprocess.run(  # noqa: S603 - absolute git path from shutil.which, list argv, no shell
            [git, *args],
            cwd=cwd,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        message = f"git {' '.join(args)} timed out after {timeout}s"
        raise GitError(message) from exc
    except OSError as exc:
        message = f"cannot run git: {exc}"
        raise GitError(message) from exc
    if result.returncode != 0:
        message = f"git {' '.join(args)} failed: {result.stderr.strip()}"
        raise GitError(message)
    return result.stdout


def _split_nul(output: str) -> list[str]:
    """Split NUL-terminated git output.

    Args:
        output: Output of a git command run with `-z`.

    Returns:
        The non-empty entries.
    """
    return [entry for entry in output.split("\0") if entry]


def _python_entries(entries: Iterable[str]) -> list[str]:
    """Keep Python files outside the excluded directories.

    Args:
        entries: Repo-relative paths.

    Returns:
        The paths worth scanning.
    """
    return [
        entry
        for entry in entries
        if entry.endswith(PYTHON_SUFFIX) and not _is_excluded(Path(entry).parts)
    ]


def tracked_targets(root: Path, timeout: float = GIT_TIMEOUT_SECONDS) -> list[Target]:
    """List every tracked Python file that still exists, unfiltered.

    Args:
        root: Repository root.
        timeout: Per-git-call timeout in seconds.

    Returns:
        One target per tracked file.
    """
    listing = run_git(["ls-files", "-z", "--", "*.py"], root, timeout)
    return [
        Target(root / entry, entry, None)
        for entry in _python_entries(_split_nul(listing))
        if (root / entry).is_file()
    ]


def changed_targets(ref: str, root: Path, timeout: float = GIT_TIMEOUT_SECONDS) -> list[Target]:
    """List Python files changed since the merge base, with their added lines.

    Args:
        ref: Base reference, compared through its merge base with HEAD.
        root: Repository root.
        timeout: Per-git-call timeout in seconds.

    Returns:
        Targets for changed tracked files (filtered to added lines) and untracked,
        non-ignored files (unfiltered).

    Raises:
        GitError: If the reference cannot be resolved or a git call fails.
    """
    try:
        base = run_git(["merge-base", ref, "HEAD"], root, timeout).strip()
    except GitError as exc:
        message = f"cannot resolve base reference {ref!r}: {exc}"
        raise GitError(message) from exc
    changed = run_git(
        ["diff", "--name-only", "-z", "--diff-filter=ACMR", base, "--"], root, timeout
    )
    targets: list[Target] = []
    for entry in _python_entries(_split_nul(changed)):
        diff = run_git(
            ["diff", "-U0", "--no-color", "--no-ext-diff", base, "--", entry], root, timeout
        )
        if (root / entry).is_file():
            targets.append(Target(root / entry, entry, parse_added_lines(diff)))
    untracked = run_git(["ls-files", "--others", "--exclude-standard", "-z"], root, timeout)
    targets.extend(
        Target(root / entry, entry, None)
        for entry in _python_entries(_split_nul(untracked))
        if (root / entry).is_file()
    )
    return targets


def _explicit_targets(raw_paths: Iterable[str]) -> list[Target]:
    """Expand explicit file and directory arguments into unfiltered targets.

    Args:
        raw_paths: Paths as given on the command line.

    Returns:
        One target per file; directories are searched recursively, skipping excluded
        directories below them.

    Raises:
        FileNotFoundError: If a path does not exist.
    """
    targets: list[Target] = []
    for raw in raw_paths:
        path = Path(raw)
        if path.is_file():
            targets.append(Target(path, raw, None))
        elif path.is_dir():
            targets.extend(
                Target(file, str(file), None)
                for file in sorted(path.rglob(f"*{PYTHON_SUFFIX}"))
                if not _is_excluded(file.relative_to(path).parts)
            )
        else:
            message = f"path not found: {raw}"
            raise FileNotFoundError(message)
    return targets


# --- command line -----------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("paths", nargs="*", help="files or directories to scan")
    parser.add_argument(
        "--changed-since",
        metavar="REF",
        help="scan files changed since the merge base of REF and HEAD; added lines only",
    )
    parser.add_argument("--tracked", action="store_true", help="scan every tracked Python file")
    return parser


def _collect_targets(paths: list[str], changed_since: str | None, *, tracked: bool) -> list[Target]:
    """Resolve the selected modes into one target per file.

    Explicit paths take precedence over git-derived targets for the same file.

    Args:
        paths: Explicit file or directory arguments.
        changed_since: Base reference for changed-lines mode, if requested.
        tracked: Whether to include every tracked file.

    Returns:
        Targets keyed by display path, in a stable order.

    Raises:
        GitError: If a git call fails.
        FileNotFoundError: If an explicit path does not exist.
    """
    by_display: dict[str, Target] = {}
    if changed_since is not None or tracked:
        root = Path(run_git(["rev-parse", "--show-toplevel"], Path.cwd()).strip())
        if tracked:
            by_display.update({t.display: t for t in tracked_targets(root)})
        if changed_since is not None:
            for target in changed_targets(changed_since, root):
                by_display.setdefault(target.display, target)
    by_display.update({t.display: t for t in _explicit_targets(paths)})
    return [by_display[key] for key in sorted(by_display)]


def _scan_all(targets: Iterable[Target]) -> tuple[list[Finding], list[ScanError]]:
    """Scan every target, collecting findings and per-file errors.

    Args:
        targets: Files to scan.

    Returns:
        All findings sorted by file and line, and all per-file errors.
    """
    findings: list[Finding] = []
    errors: list[ScanError] = []
    for target in targets:
        try:
            findings.extend(_scan_target(target))
        except ScanFailure as failure:
            errors.append(ScanError(target.display, failure.line, failure.message))
    findings.sort(key=lambda f: (f.file, f.line, f.pattern_id, f.text))
    return findings, errors


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check.

    Prints one line per finding, then a summary line when anything was found.

    Args:
        argv: Command-line arguments; defaults to `sys.argv[1:]`.

    Returns:
        0 when clean, 1 when there are findings, 2 on a usage or environment error.
    """
    args = _build_parser().parse_args(argv)
    paths = cast("list[str]", args.paths)
    changed_since = cast("str | None", args.changed_since)
    tracked = cast("bool", args.tracked)
    if not (paths or changed_since is not None or tracked):
        sys.stderr.write("error: give paths, --changed-since REF or --tracked\n")
        return EXIT_ERROR
    try:
        targets = _collect_targets(paths, changed_since, tracked=tracked)
    except (GitError, FileNotFoundError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_ERROR
    findings, errors = _scan_all(targets)
    for finding in findings:
        sys.stdout.write(f"{finding.format()}\n")
    for error in errors:
        sys.stdout.write(f"{error.format()}\n")
    if findings:
        files = len({f.file for f in findings})
        sys.stdout.write(f"{len(findings)} finding(s) in {files} file(s)\n")
    if errors:
        return EXIT_ERROR
    return EXIT_FINDINGS if findings else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
