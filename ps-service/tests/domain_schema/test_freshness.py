"""`check` compares every generated artifact with the schema and reports drift (AC-BI-006/008)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

from ps_service.domain_schema.__main__ import build_parser, main
from ps_service.domain_schema.freshness import CANONICAL_DOC, PACKAGED_DOC

_REPO = Path(__file__).resolve().parents[3]
_INTAKE_COPIES = (
    "docs/artifacts/schemas/internal-regulation-intake.v1.schema.json",
    "ps-service/src/ps_service/api/schemas/internal_regulation_intake_v1.schema.json",
    "ps-cli/src/ps_cli/schemas/internal_regulation_intake_v1.schema.json",
)
_ARTIFACTS = (CANONICAL_DOC, PACKAGED_DOC, *_INTAKE_COPIES)
_HOOK_ID = "domain-schema-freshness"
_CHECK_ID = "verify-domain-schema-freshness"
_EXPECTED_SUBCOMMANDS = {"check", "write-docs", "print-intake", "measure"}
_SOURCE_PATHS = (
    "ps-service/src/ps_service/domain_schema/definition.py",
    "ps-service/src/ps_service/domain_schema/render_doc.py",
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A repo-shaped tree holding copies of the real artifacts."""
    for relative in _ARTIFACTS:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REPO / relative, target)
    return tmp_path


def _check(root: Path) -> int:
    return main(["check", "--repo-root", str(root)])


def _flip_one_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    index = data.index(b"0")
    data[index : index + 1] = b"1"
    path.write_bytes(bytes(data))


def test_committed_tree_is_fresh() -> None:
    """AC-BI-006: the real repository passes its own freshness check."""
    assert _check(_REPO) == 0


def test_fresh_copy_exits_zero_and_prints_nothing(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _check(tree) == 0
    assert capsys.readouterr().err == ""


def test_edited_table_cell_in_the_canonical_doc_is_drift(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tree / CANONICAL_DOC
    doc.write_text(doc.read_text("utf-8").replace("| 1 : 0..* |", "| 9 : 0..* |", 1), "utf-8")

    assert _check(tree) == 1

    err = capsys.readouterr().err
    assert f"event=domain_schema_drift artifact={CANONICAL_DOC}" in err
    assert "-" in err
    assert "9 : 0..*" in err


def test_a_region_missing_from_the_doc_is_drift(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tree / CANONICAL_DOC
    text = doc.read_text("utf-8")
    doc.write_text(re.sub(r"<!-- (BEGIN|END) GENERATED properties:Role[^\n]*\n", "", text), "utf-8")

    assert _check(tree) == 1
    assert f"artifact={CANONICAL_DOC}" in capsys.readouterr().err


def test_stale_packaged_copy_is_drift(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    packaged = tree / PACKAGED_DOC
    packaged.write_text(packaged.read_text("utf-8") + "\nstale\n", "utf-8")

    assert _check(tree) == 1
    assert f"event=domain_schema_drift artifact={PACKAGED_DOC}" in capsys.readouterr().err


@pytest.mark.parametrize("relative", _INTAKE_COPIES)
def test_one_byte_change_in_an_intake_copy_is_drift(
    tree: Path, capsys: pytest.CaptureFixture[str], relative: str
) -> None:
    """AC-BI-006: each of the three committed intake schema copies is compared."""
    _flip_one_byte(tree / relative)

    assert _check(tree) == 1
    assert f"event=domain_schema_drift artifact={relative}" in capsys.readouterr().err


@pytest.mark.parametrize("relative", _ARTIFACTS)
def test_missing_artifact_exits_two(
    tree: Path, capsys: pytest.CaptureFixture[str], relative: str
) -> None:
    (tree / relative).unlink()

    assert _check(tree) == 2
    assert f"event=domain_schema_missing artifact={relative}" in capsys.readouterr().err


def test_reports_name_no_absolute_path(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _flip_one_byte(tree / _INTAKE_COPIES[0])
    doc = tree / CANONICAL_DOC
    doc.write_text(doc.read_text("utf-8").replace("| 1 : 0..* |", "| 9 : 0..* |", 1), "utf-8")

    _check(tree)

    err = capsys.readouterr().err
    assert str(tree) not in err
    assert str(_REPO) not in err


def test_check_never_modifies_the_artifacts(tree: Path) -> None:
    _flip_one_byte(tree / _INTAKE_COPIES[1])
    before = {relative: (tree / relative).read_bytes() for relative in _ARTIFACTS}

    _check(tree)

    assert {relative: (tree / relative).read_bytes() for relative in _ARTIFACTS} == before


def test_subcommand_set_is_exactly_check_write_docs_print_intake_measure() -> None:
    """F4: the parser offers these four and no write-intake variant."""
    parser = build_parser()
    [action] = [a for a in parser._actions if a.dest == "command"]  # pyright: ignore[reportPrivateUsage]  # argparse exposes its subcommands only through the private action list

    assert set(action.choices or ()) == _EXPECTED_SUBCOMMANDS


def _precommit_hook() -> dict[str, object]:
    config = yaml.safe_load((_REPO / ".pre-commit-config.yaml").read_text("utf-8"))
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]
    [hook] = [hook for hook in hooks if hook["id"] == _HOOK_ID]
    return hook


@pytest.mark.parametrize("relative", [*_ARTIFACTS, *_SOURCE_PATHS])
def test_precommit_hook_triggers_on_every_artifact_and_source_path(relative: str) -> None:
    """AC-BI-008: the hook's `files` regex matches each artifact the check compares."""
    pattern = str(_precommit_hook()["files"])

    assert re.search(pattern, relative)


def test_precommit_hook_runs_the_check_without_filenames() -> None:
    hook = _precommit_hook()

    assert str(hook["entry"]).endswith(" check")
    assert hook["pass_filenames"] is False


def test_ci_defines_the_check_and_lists_it_in_trunk_worthy() -> None:
    """AC-BI-008: `.insitu.yml` runs the same command in the trunk-worthy wave."""
    config = yaml.safe_load((_REPO / ".insitu.yml").read_text("utf-8"))
    [check] = [c for c in config["inventory"] if c["id"] == _CHECK_ID]
    [trunk_worthy] = [c for c in config["waves"] if c["id"] == "trunk-worthy"]

    assert check["command"] == "uv run python -m ps_service.domain_schema check"
    assert _CHECK_ID in trunk_worthy["checks"]
