"""Tests for `scripts/check_config_drift.py`.

The check compares configuration used by code with what the chart, the scripts and the
documentation declare. Tests build small real repository trees in `tmp_path`, inject one
violation per rule and run the script's `main`; nothing is mocked.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import types

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_config_drift.py"
CHART = "charts/policy-system"
REFERENCE = "docs/artifacts/helm-chart-values-reference.md"

VALUES = "psService:\n  port: 8000\n  mode: fast\n"
PROD_VALUES = "psService:\n  mode: strict\n"
TEMPLATE = "port: {{ .Values.psService.port }}\nmode: {{ .Values.psService.mode }}\n"
REFERENCE_TEXT = "| `psService.port` | `8000` | Port. |\n| `psService.mode` | `fast` | Mode. |\n"
CODE = 'PORT = "PS_PORT"\n'
DEPLOY_TEXT = "PS_PORT=8000\n"


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("check_config_drift", SCRIPT_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        message = f"could not load spec for {SCRIPT_PATH}"
        raise ImportError(message)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


drift = _load_script()


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small repository tree on which every rule passes."""
    _write(tmp_path, f"{CHART}/values.yaml", VALUES)
    _write(tmp_path, f"{CHART}/values-prod.yaml", PROD_VALUES)
    _write(tmp_path, f"{CHART}/templates/deployment.yaml", TEMPLATE)
    _write(tmp_path, REFERENCE, REFERENCE_TEXT)
    _write(tmp_path, "ps-service/src/ps_service/config.py", CODE)
    _write(tmp_path, ".env.example", DEPLOY_TEXT)
    return tmp_path


def _run(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = drift.main(["--root", str(root)])
    return code, capsys.readouterr().out


def test_clean_tree_exits_zero_and_prints_nothing(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(tree, capsys)

    assert code == 0
    assert out == ""


def test_env_var_read_by_code_but_listed_nowhere_fails_naming_the_rule(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tree, "ps-service/src/ps_service/extra.py", 'X = "PS_UNLISTED_SETTING"\n')

    code, out = _run(tree, capsys)

    assert code == 1
    assert "RULE env-var-unlisted" in out
    assert "ps-service/src/ps_service/extra.py:1" in out
    assert "PS_UNLISTED_SETTING" in out
    assert "How to fix" in out


def test_env_var_named_only_by_a_document_is_accepted(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tree, "ps-service/src/ps_service/extra.py", 'X = "PS_DOCUMENTED_SETTING"\n')
    _write(tree, "docs/guide.md", "Set `PS_DOCUMENTED_SETTING` to tune it.\n")

    code, out = _run(tree, capsys)

    assert (code, out) == (0, "")


def test_env_var_named_only_in_an_audit_report_still_fails(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tree, "ps-service/src/ps_service/extra.py", 'X = "PS_AUDIT_ONLY"\n')
    _write(tree, "docs/audits/report.md", "PS_AUDIT_ONLY appears here.\n")

    code, out = _run(tree, capsys)

    assert code == 1
    assert "RULE env-var-unlisted" in out


def test_env_var_prefix_ending_in_underscore_is_not_a_variable_name(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tree, "ps-service/src/ps_service/extra.py", 'PREFIX = "PS_"\nOTHER = "PS_SECTION_"\n')

    code, out = _run(tree, capsys)

    assert (code, out) == (0, "")


def test_chart_value_no_template_reads_fails_naming_the_rule(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tree, f"{CHART}/values.yaml", VALUES + "  unusedKnob: true\n")
    _write(tree, REFERENCE, REFERENCE_TEXT + "| `psService.unusedKnob` | `true` | Knob. |\n")

    code, out = _run(tree, capsys)

    assert code == 1
    assert "RULE helm-value-unused" in out
    assert "psService.unusedKnob" in out
    assert f"{CHART}/values.yaml:4" in out


def test_production_override_missing_from_default_values_fails_naming_the_rule(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tree, f"{CHART}/values-prod.yaml", PROD_VALUES + "  stray: 1\n")
    _write(tree, REFERENCE, REFERENCE_TEXT + "| `psService.stray` | `1` | Stray. |\n")

    code, out = _run(tree, capsys)

    assert code == 1
    assert "RULE helm-prod-orphan" in out
    assert "psService.stray" in out


def test_production_override_in_the_pass_through_baseline_is_accepted(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baselined = "authentik.global.env"
    assert baselined in drift.PROD_ORPHAN_BASELINE
    _write(tree, f"{CHART}/values-prod.yaml", PROD_VALUES + "authentik:\n  global:\n    env: 1\n")
    _write(tree, REFERENCE, REFERENCE_TEXT + f"| `{baselined}` | | Pass-through. |\n")

    code, out = _run(tree, capsys)

    assert (code, out) == (0, "")


def test_chart_value_missing_from_the_reference_fails_naming_the_rule(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tree, f"{CHART}/values.yaml", VALUES + "  timeout: 5\n")
    _write(tree, f"{CHART}/templates/probe.yaml", "t: {{ .Values.psService.timeout }}\n")

    code, out = _run(tree, capsys)

    assert code == 1
    assert "RULE helm-value-undocumented" in out
    assert "psService.timeout" in out


def test_top_level_value_documented_by_a_single_segment_name_is_accepted(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tree, f"{CHART}/values.yaml", VALUES + "nameOverride: ''\n")
    _write(tree, f"{CHART}/templates/names.yaml", "n: {{ .Values.nameOverride }}\n")
    _write(tree, REFERENCE, REFERENCE_TEXT + "| `nameOverride` | `''` | Name. |\n")

    code, out = _run(tree, capsys)

    assert (code, out) == (0, "")


def test_tree_without_values_file_exits_with_usage_error(tmp_path: Path) -> None:
    assert drift.main(["--root", str(tmp_path)]) == 2


def test_failure_line_has_the_documented_shape(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tree, "ps-service/src/ps_service/extra.py", 'X = "PS_SHAPE_CHECK"\n')

    _, out = _run(tree, capsys)

    assert out.startswith("check_config_drift.py: RULE env-var-unlisted: ")
    assert out.rstrip().endswith("See CONTRIBUTING.md#automated-guardrails.")


def test_checked_out_tree_has_no_configuration_drift(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(REPO_ROOT, capsys)

    assert out == ""
    assert code == 0
