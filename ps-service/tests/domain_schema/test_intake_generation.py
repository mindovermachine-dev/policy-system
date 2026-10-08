"""The generated intake schema equals the committed file byte for byte (AC-BI-005).

The generator only produces text; nothing in this package writes the committed intake
schema or its vendored copies (D13), so `print-intake` is the only way to see it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ps_service.domain_schema import DOMAIN_SCHEMA
from ps_service.domain_schema.__main__ import build_parser, main
from ps_service.domain_schema.intake import generate_intake_schema

_REPO = Path(__file__).resolve().parents[3]
_DOCS_COPY = _REPO / "docs" / "artifacts" / "schemas" / "internal-regulation-intake.v1.schema.json"
_SERVICE_COPY = (
    _REPO / "ps-service/src/ps_service/api/schemas/internal_regulation_intake_v1.schema.json"
)
_CLI_COPY = _REPO / "ps-cli/src/ps_cli/schemas/internal_regulation_intake_v1.schema.json"
_FORBIDDEN_SUBCOMMANDS = ("write-intake", "write-intake-schema")


def test_generated_intake_schema_is_byte_identical_to_committed() -> None:
    """AC-BI-005: generation from `DOMAIN_SCHEMA` reproduces the committed bytes."""
    committed = _DOCS_COPY.read_bytes()

    assert generate_intake_schema(DOMAIN_SCHEMA).encode("utf-8") == committed


def test_generated_intake_schema_is_valid_json_with_the_committed_top_level_order() -> None:
    parsed = json.loads(generate_intake_schema(DOMAIN_SCHEMA))

    assert list(parsed) == list(json.loads(_DOCS_COPY.read_text(encoding="utf-8")))
    assert list(parsed["$defs"]) == list(
        json.loads(_DOCS_COPY.read_text(encoding="utf-8"))["$defs"]
    )


def test_three_committed_copies_are_identical() -> None:
    """AC-BI-005: docs, ps-service and ps-cli copies hold the same bytes."""
    docs = _DOCS_COPY.read_bytes()

    assert _SERVICE_COPY.read_bytes() == docs
    assert _CLI_COPY.read_bytes() == docs


def test_generator_does_not_write_files(capsys: pytest.CaptureFixture[str]) -> None:
    """D13/F4: the CLI can print the intake schema but has no write-intake subcommand."""
    parser = build_parser()

    assert parser.parse_args(["print-intake"]).command == "print-intake"
    for name in _FORBIDDEN_SUBCOMMANDS:
        with pytest.raises(SystemExit):
            parser.parse_args([name])
    assert "invalid choice" in capsys.readouterr().err


def test_print_intake_prints_the_committed_bytes(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["print-intake"])

    assert exit_code == 0
    assert capsys.readouterr().out == _DOCS_COPY.read_text(encoding="utf-8")


def test_print_intake_does_not_modify_the_committed_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = [path.read_bytes() for path in (_DOCS_COPY, _SERVICE_COPY, _CLI_COPY)]

    main(["print-intake"])
    capsys.readouterr()

    assert [path.read_bytes() for path in (_DOCS_COPY, _SERVICE_COPY, _CLI_COPY)] == before
