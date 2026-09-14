"""Tests for `tools/domain-mapper/exclusion_audit.py`'s CLI surface (issue #27, Slice 5,
PLAN.md §3 "Slice 5 — CLI polish + AC-BI-002 close-out").

No new capture/report logic under test here — Slice 4 already proved `main()`'s cached-mode
report assembly (`test_exclusion_audit_report.py`'s
`test_main_cached_mode_assembles_report_from_json_with_zero_extraction_calls`). This file
covers only what Slice 5 adds: `--help` text, `--cache-dir`/`--report-path`/
`--record-source` argument parsing, and the cached-by-default guarantee that running this
script's CLI with no flags at all can never reach for a live Azure OpenAI or FalkorDB call.

Loads `exclusion_audit.py` by path, same pattern as the sibling `test_exclusion_audit_*.py`
files (`tools/domain-mapper/` is hyphenated, not an importable dotted package).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_TOOLS_DOMAIN_MAPPER_DIR = Path(__file__).resolve().parents[3] / "tools" / "domain-mapper"
_SCRIPT_PATH = _TOOLS_DOMAIN_MAPPER_DIR / "exclusion_audit.py"
_MODULE_NAME = "_exclusion_audit_cli_under_test"


def _load_exclusion_audit_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MODULE_NAME]
        raise
    return module


@pytest.fixture
def cli_module() -> ModuleType:
    return _load_exclusion_audit_module()


def _forbidden_call_completion(*, model: str, messages: object, timeout: float) -> object:
    raise AssertionError(
        "cached-mode main() must never call the LLM -- this call_completion should never be invoked"
    )


# --- --help ------------------------------------------------------------------


def test_help_exits_zero_and_documents_every_flag(
    cli_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--help` is the first thing a maintainer reaches for -- it must exit 0 (not error)
    and name every real flag, so `--help` alone is enough to learn this script never
    defaults to a live run.
    """
    with pytest.raises(SystemExit) as exc_info:
        cli_module._parse_args(["--help"])  # pyright: ignore[reportPrivateUsage] -- internal parser under test

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--cache-dir" in help_text
    assert "--report-path" in help_text
    assert "--record-source" in help_text
    assert "cached" in help_text.lower()


def test_help_mentions_the_cached_only_no_live_call_guarantee(
    cli_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point of Slice 5's CLI polish is that reading `--help` alone tells a
    maintainer this script cannot accidentally spend real Azure OpenAI/FalkorDB calls.
    """
    with pytest.raises(SystemExit):
        cli_module._parse_args(["--help"])  # pyright: ignore[reportPrivateUsage] -- internal parser under test

    help_text = capsys.readouterr().out.lower()
    assert "live" in help_text
    assert "never" in help_text


# --- Argument defaults ---------------------------------------------------------


def test_record_source_defaults_to_cached(cli_module: ModuleType) -> None:
    args = cli_module._parse_args([])  # pyright: ignore[reportPrivateUsage] -- internal parser under test

    assert args.record_source == "cached"


def test_cache_dir_and_report_path_default_to_the_module_constants(
    cli_module: ModuleType,
) -> None:
    args = cli_module._parse_args([])  # pyright: ignore[reportPrivateUsage] -- internal parser under test

    assert args.records_dir == cli_module._DEFAULT_RECORDS_DIR  # pyright: ignore[reportPrivateUsage]
    assert args.report_path == cli_module._DEFAULT_REPORT_PATH  # pyright: ignore[reportPrivateUsage]


def test_record_source_rejects_anything_other_than_cached(cli_module: ModuleType) -> None:
    """No live mode is implemented (CHANGES.md row 1b) -- argparse's own `choices=` must
    reject e.g. `--record-source live` at parse time, before `main()`'s body (and any
    chance of a live call) ever runs.
    """
    with pytest.raises(SystemExit) as exc_info:
        cli_module._parse_args(["--record-source", "live"])  # pyright: ignore[reportPrivateUsage]

    assert exc_info.value.code != 0


def test_cache_dir_and_report_path_flags_override_defaults(
    cli_module: ModuleType, tmp_path: Path
) -> None:
    custom_cache_dir = tmp_path / "custom-records"
    custom_report_path = tmp_path / "custom-report.md"

    args = cli_module._parse_args(  # pyright: ignore[reportPrivateUsage]
        [
            "--cache-dir",
            str(custom_cache_dir),
            "--report-path",
            str(custom_report_path),
        ]
    )

    assert args.records_dir == custom_cache_dir
    assert args.report_path == custom_report_path


# --- Cached-by-default main() behavior (no argv flags at all) ------------------


def _write_minimal_regulation_fixture(
    records_dir: Path, *, regulation: str, filename: str, citation_ref: str
) -> None:
    payload = {
        "records": [
            {
                "regulation": regulation,
                "citation_ref": citation_ref,
                "article_number": "1",
                "paragraph_number": "1",
                "candidate_count": 1,
                "status": "ok",
                "is_flagged": False,
                "classification": None,
            }
        ]
    }
    (records_dir / filename).write_text(json.dumps(payload), encoding="utf-8")


def test_main_with_no_flags_at_all_stays_cached_and_never_calls_the_llm(
    cli_module: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core Slice 5 guarantee: `main([])` -- i.e. every flag left at its default,
    the shape of running this script with no arguments -- must still never reach for
    `call_completion`. Points `--cache-dir`'s own default at a tmp fixture via
    monkeypatching the module constant (rather than writing into the real
    `.orchestrator/tracker/issue-27-exclusion-audit/records/`), so this test never
    touches the real captured audit data.
    """
    records_dir = tmp_path / "records"
    records_dir.mkdir()
    report_path = tmp_path / "findings.md"

    for regulation, filename, citation_ref in (
        ("CRA", "cra.json", "CRA Art. 1"),
        ("GDPR", "gdpr.json", "GDPR Art. 1"),
        ("NIS2", "nis2.json", "NIS2 Art. 1"),
    ):
        _write_minimal_regulation_fixture(
            records_dir, regulation=regulation, filename=filename, citation_ref=citation_ref
        )

    monkeypatch.setattr(cli_module, "_DEFAULT_RECORDS_DIR", records_dir)
    monkeypatch.setattr(cli_module, "_DEFAULT_REPORT_PATH", report_path)

    exit_code = cli_module.main([], call_completion=_forbidden_call_completion)

    assert exit_code == 0
    assert report_path.exists()
    report_text = report_path.read_text(encoding="utf-8")
    assert "CRA Art. 1" in report_text
    assert "GDPR Art. 1" in report_text
    assert "NIS2 Art. 1" in report_text


def test_main_explicit_cached_record_source_flag_matches_default_behavior(
    cli_module: ModuleType, tmp_path: Path
) -> None:
    """`--record-source cached` given explicitly must behave identically to leaving it
    unset -- the flag exists for documentation/forward-compat (PLAN.md §3 Slice 5), not
    to change behavior.
    """
    records_dir = tmp_path / "records"
    records_dir.mkdir()
    report_path = tmp_path / "findings.md"

    for regulation, filename, citation_ref in (
        ("CRA", "cra.json", "CRA Art. 1"),
        ("GDPR", "gdpr.json", "GDPR Art. 1"),
        ("NIS2", "nis2.json", "NIS2 Art. 1"),
    ):
        _write_minimal_regulation_fixture(
            records_dir, regulation=regulation, filename=filename, citation_ref=citation_ref
        )

    exit_code = cli_module.main(
        [
            "--cache-dir",
            str(records_dir),
            "--report-path",
            str(report_path),
            "--record-source",
            "cached",
        ],
        call_completion=_forbidden_call_completion,
    )

    assert exit_code == 0
    assert report_path.exists()
