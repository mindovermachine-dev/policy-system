"""Command line for the domain schema: `python -m ps_service.domain_schema <command>`.

Commands: `check` regenerates every artifact in memory and exits 1 on drift (2 when an artifact
is missing); `write-docs` regenerates the marked regions of the domain-concepts document and its
packaged copy; `print-intake` prints the generated intake JSON Schema to stdout (it never writes
files); `measure` prints the character count of the packaged domain-concepts document
and of the slim schema the `domain_concepts` tool serves (AC-BI-011).
"""

from __future__ import annotations

import argparse
import sys
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from ps_service.domain_schema.definition import DOMAIN_SCHEMA
from ps_service.domain_schema.errors import MissingArtifactError
from ps_service.domain_schema.freshness import check_artifacts, write_docs
from ps_service.domain_schema.intake import generate_intake_schema
from ps_service.domain_schema.render_slim import render_slim_schema

if TYPE_CHECKING:
    from collections.abc import Sequence

_DOCUMENT_NAME = "ps-domain-concepts.md"
_EXIT_DRIFT = 1
_EXIT_USAGE = 2


def _measure() -> int:
    document = files("ps_service.mcp_interface").joinpath(_DOCUMENT_NAME).read_text("utf-8")
    slim = render_slim_schema(DOMAIN_SCHEMA)
    sys.stdout.write(f"before={len(document)} after={len(slim)}\n")
    return 0


def _print_intake() -> int:
    sys.stdout.write(generate_intake_schema(DOMAIN_SCHEMA))
    return 0


def _check(repo_root: Path) -> int:
    try:
        drifts = check_artifacts(repo_root)
    except MissingArtifactError as error:
        sys.stderr.write(f"event=domain_schema_missing artifact={error.artifact}\n")
        return _EXIT_USAGE
    for drift in drifts:
        sys.stderr.write(drift.diff)
        sys.stderr.write(f"event=domain_schema_drift artifact={drift.artifact}\n")
    return _EXIT_DRIFT if drifts else 0


def _write_docs(repo_root: Path) -> int:
    write_docs(repo_root)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser; one subcommand per supported action."""
    parser = argparse.ArgumentParser(prog="python -m ps_service.domain_schema")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("check", "exit 1 when a generated artifact has drifted from the schema"),
        ("write-docs", "regenerate the marked document regions"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--repo-root", type=Path, default=Path.cwd(), help="repository root")
    commands.add_parser("print-intake", help="print the generated intake JSON Schema")
    commands.add_parser("measure", help="print before/after character counts")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command named in `argv` and return the process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return _check(args.repo_root)
    if args.command == "write-docs":
        return _write_docs(args.repo_root)
    if args.command == "print-intake":
        return _print_intake()
    if args.command == "measure":
        return _measure()
    raise AssertionError(args.command)  # pragma: no cover  # argparse rejects unknown commands


if __name__ == "__main__":
    sys.exit(main())
