"""Flag drift between configuration declared in code, the Helm chart and the documentation.

Four rules, each reported with its rule id:

- `env-var-unlisted`: a `PS_*` environment variable name used in production code that appears
  nowhere in the chart, the scripts, the CI configuration, `.env.example` or the documentation.
- `helm-value-unused`: a leaf key of `charts/policy-system/values.yaml` that no chart template
  reads as `.Values.<path>`.
- `helm-prod-orphan`: a key of `values-prod.yaml` that `values.yaml` does not declare. Keys that
  pass through to a sub-chart are listed in `PROD_ORPHAN_BASELINE` with the reason.
- `helm-value-undocumented`: a leaf key of either values file that the values reference
  document does not name in backticks.

The scan reads files as text and parses the values files with `yaml.safe_load`. It never
imports or executes scanned code.

Usage: `uv run python scripts/check_config_drift.py [--root PATH]`

Exit codes: 0 clean, 1 at least one finding, 2 the tree lacks a file the check needs.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

CODE_ROOTS = ("ps-service/src", "ps-cli/src", "ps-test-support/src")
LISTING_PATHS = (
    "charts",
    "scripts",
    "deploy",
    ".github",
    ".env.example",
    "docs",
    "README.md",
    "ps-skills",
)
SKIPPED_DIRS = frozenset({"__pycache__", "node_modules", ".venv", ".git", "audits"})
SKIPPED_SUFFIXES = (".pyc", ".png", ".gz", ".tgz", ".jpg", ".ico")
CHART_DIR = "charts/policy-system"
REFERENCE_DOC = "docs/artifacts/helm-chart-values-reference.md"
PROD_ORPHAN_BASELINE: dict[str, str] = {
    "authentik.global.env": "passed through to the Authentik sub-chart, which defines the key",
}

_ENV_PATTERN = re.compile(r"\b(PS_[A-Z0-9_]+)\b")
_VALUES_REF_PATTERN = re.compile(r"\.Values\.([A-Za-z0-9_.]+)")
_BACKTICKED_KEY_PATTERN = re.compile(r"`([A-Za-z][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]+)*)`")
_GUIDE = "See CONTRIBUTING.md#automated-guardrails."


@dataclass(frozen=True, slots=True)
class Finding:
    """A single configuration drift finding.

    Attributes:
        rule: Rule id printed with the finding.
        file: Repository-relative path the finding points at.
        line: 1-based line number, or 1 when the position is not known.
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
            f"check_config_drift.py: RULE {self.rule}: {self.file}:{self.line}: "
            f"{self.message}. How to fix: {self.fix}. {_GUIDE}"
        )


def _text_files(root: Path, relative: str) -> Iterator[Path]:
    """Yield the readable text files under a path of the tree.

    Args:
        root: Repository root.
        relative: File or directory, relative to the root.

    Yields:
        Each file whose path avoids the skipped directories and suffixes.
    """
    target = root / relative
    if target.is_file():
        yield target
        return
    if not target.is_dir():
        return
    for path in sorted(target.rglob("*")):
        parts = set(path.relative_to(root).parts)
        if path.is_file() and not parts & SKIPPED_DIRS and not path.name.endswith(SKIPPED_SUFFIXES):
            yield path


def _read(path: Path) -> str:
    """Read a file as text, treating undecodable files as empty.

    Args:
        path: File to read.

    Returns:
        The file text, or an empty string when it is not valid UTF-8.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def _leaves(node: dict[str, Any], prefix: str = "") -> Iterator[str]:
    """Yield the dotted path of every leaf in a nested mapping.

    Args:
        node: Parsed YAML mapping.
        prefix: Dotted path of the mapping itself.

    Yields:
        One dotted path per non-mapping (or empty-mapping) value.
    """
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            yield from _leaves(cast("dict[str, Any]", value), path)
        else:
            yield path


def _load_values(root: Path, name: str) -> dict[str, Any]:
    """Parse one chart values file.

    Args:
        root: Repository root.
        name: File name inside the chart directory.

    Returns:
        The parsed mapping (empty for an empty file).

    Raises:
        FileNotFoundError: The values file does not exist.
    """
    path = root / CHART_DIR / name
    loaded = cast("dict[str, Any] | None", yaml.safe_load(path.read_text(encoding="utf-8")))
    return loaded or {}


def _key_line(root: Path, relative: str, dotted: str) -> int:
    """Locate the line declaring the last segment of a dotted key.

    Args:
        root: Repository root.
        relative: File to search.
        dotted: Dotted key path.

    Returns:
        The 1-based line of the first declaration of the last segment, or 1.
    """
    leaf = dotted.rsplit(".", maxsplit=1)[-1]
    pattern = re.compile(rf"^\s*{re.escape(leaf)}\s*:")
    for number, line in enumerate(_read(root / relative).splitlines(), start=1):
        if pattern.match(line):
            return number
    return 1


def _related(path: str, others: set[str]) -> bool:
    """Tell whether a dotted path equals, contains or sits inside any other path.

    Args:
        path: Dotted path to test.
        others: Dotted paths to compare against.

    Returns:
        True when some other path is the same, a parent or a child of the path.
    """
    return any(
        other == path or other.startswith(f"{path}.") or path.startswith(f"{other}.")
        for other in others
    )


def find_env_var_unlisted(root: Path) -> list[Finding]:
    """Find production env var names that no deployment or documentation file mentions.

    Args:
        root: Repository root.

    Returns:
        One finding per undeclared name, at its first use in code.
    """
    listed: set[str] = set()
    for relative in LISTING_PATHS:
        for path in _text_files(root, relative):
            listed.update(_ENV_PATTERN.findall(_read(path)))
    first_use: dict[str, tuple[str, int]] = {}
    for relative in CODE_ROOTS:
        for path in _text_files(root, relative):
            for number, line in enumerate(_read(path).splitlines(), start=1):
                for name in _ENV_PATTERN.findall(line):
                    if not name.endswith("_"):
                        first_use.setdefault(name, (str(path.relative_to(root)), number))
    return [
        Finding(
            "env-var-unlisted",
            file,
            line,
            f"environment variable {name} is read by code but is not set or documented anywhere",
            "add it to the chart or .env.example and the documentation, or remove the use",
        )
        for name, (file, line) in sorted(first_use.items())
        if name not in listed
    ]


def find_helm_value_unused(root: Path) -> list[Finding]:
    """Find `values.yaml` leaves that no chart template reads.

    Args:
        root: Repository root.

    Returns:
        One finding per unreferenced leaf.
    """
    templates = "".join(_read(path) for path in _text_files(root, f"{CHART_DIR}/templates"))
    references = set(_VALUES_REF_PATTERN.findall(templates))
    relative = f"{CHART_DIR}/values.yaml"
    return [
        Finding(
            "helm-value-unused",
            relative,
            _key_line(root, relative, leaf),
            f"values key {leaf} is not read by any chart template",
            "reference it from a template or delete the key",
        )
        for leaf in _leaves(_load_values(root, "values.yaml"))
        if not _related(leaf, references)
    ]


def find_helm_prod_orphan(root: Path) -> list[Finding]:
    """Find production override keys that the default values file does not declare.

    Args:
        root: Repository root.

    Returns:
        One finding per undeclared key that is not in the baseline.
    """
    declared = set(_leaves(_load_values(root, "values.yaml")))
    relative = f"{CHART_DIR}/values-prod.yaml"
    return [
        Finding(
            "helm-prod-orphan",
            relative,
            _key_line(root, relative, leaf),
            f"production override {leaf} has no counterpart in values.yaml",
            "declare the key in values.yaml or add it to PROD_ORPHAN_BASELINE with a reason",
        )
        for leaf in _leaves(_load_values(root, "values-prod.yaml"))
        if leaf not in PROD_ORPHAN_BASELINE and not _related(leaf, declared)
    ]


def find_helm_value_undocumented(root: Path) -> list[Finding]:
    """Find values leaves that the values reference document never names.

    Args:
        root: Repository root.

    Returns:
        One finding per undocumented leaf of either values file.
    """
    named = set(_BACKTICKED_KEY_PATTERN.findall(_read(root / REFERENCE_DOC)))
    findings: list[Finding] = []
    seen: set[str] = set()
    for name in ("values.yaml", "values-prod.yaml"):
        relative = f"{CHART_DIR}/{name}"
        for leaf in _leaves(_load_values(root, name)):
            if leaf in seen or _related(leaf, named):
                continue
            seen.add(leaf)
            findings.append(
                Finding(
                    "helm-value-undocumented",
                    relative,
                    _key_line(root, relative, leaf),
                    f"values key {leaf} is not named in {REFERENCE_DOC}",
                    "add a row for the key to the values reference",
                )
            )
    return findings


def run_checks(root: Path) -> list[Finding]:
    """Run every rule against a repository tree.

    Args:
        root: Repository root.

    Returns:
        All findings, in rule order.
    """
    return [
        *find_env_var_unlisted(root),
        *find_helm_value_unused(root),
        *find_helm_prod_orphan(root),
        *find_helm_value_undocumented(root),
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
        findings = run_checks(root)
    except FileNotFoundError as error:
        sys.stderr.write(f"check_config_drift.py: missing input file: {error.filename}\n")
        return EXIT_ERROR
    for finding in findings:
        sys.stdout.write(finding.render() + "\n")
    return EXIT_FINDINGS if findings else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
