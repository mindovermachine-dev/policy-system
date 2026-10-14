"""Every `ps-cli ... --help` screen is end-user text and must carry no provenance.

`--help` output ships to operators, so it must not cite acceptance-criterion ids,
issue or pull-request numbers, planning documents or tracker paths. The test renders
the help of the top-level parser and of every subcommand, normalises whitespace
(argparse wraps long phrases across lines), and checks the shared provenance patterns.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ps_cli.modules.parser import build_parser

if TYPE_CHECKING:
    import types
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "check_docstring_provenance.py"
_SUBCOMMAND_LINE = re.compile(r"^  \{([\w,.-]+)\}", re.MULTILINE)
_EXPECTED_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        (),
        ("get",),
        ("get", "health"),
        ("ingest",),
        ("ingest", "document"),
        ("config",),
        ("config", "set-context"),
        ("config", "use-context"),
        ("config", "get-contexts"),
        ("auth",),
        ("auth", "login"),
        ("auth", "status"),
        ("auth", "logout"),
        ("export",),
        ("export", "instrument"),
    }
)


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_docstring_provenance", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_SCRIPT_MODULE = _load_script()


def _walk(
    path: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> Iterator[tuple[tuple[str, ...], str]]:
    """Yield `(command path, help text)` for `path` and every subcommand beneath it."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([*path, "--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    yield path, text
    for group in _SUBCOMMAND_LINE.findall(text):
        for name in group.split(","):
            yield from _walk((*path, name), capsys)


def _violations(text: str) -> list[str]:
    normalised = re.sub(r"\s+", " ", _SCRIPT_MODULE._URL_PATTERN.sub(" ", text))  # pyright: ignore[reportPrivateUsage]  # reuses the checker's URL stripping
    return [
        f"{pattern.pattern_id}: {match.group(0)}"
        for pattern in _SCRIPT_MODULE.PATTERNS
        if (match := pattern.regex.search(normalised)) is not None
    ]


def test_every_help_screen_is_provenance_free(capsys: pytest.CaptureFixture[str]) -> None:
    """No `--help` screen of any ps-cli command cites an issue number or planning doc."""
    screens = list(_walk((), capsys))

    found_paths = {path for path, _ in screens}
    assert found_paths >= _EXPECTED_PATHS
    violations = [
        f"ps-cli {' '.join(path)} --help -> {violation}"
        for path, text in screens
        for violation in _violations(text)
    ]
    assert violations == []


@pytest.mark.parametrize(
    "fragment",
    ["AC" + "-BI-001", "issue " + "#12", "PLAN" + ".md", "spike" + "-3"],
)
def test_matcher_flags_provenance_fragments(fragment: str) -> None:
    """The matcher is live: each provenance shape, wrapped in help-like prose, is flagged."""
    assert _violations(f"Some help text\n {fragment}.") != []
