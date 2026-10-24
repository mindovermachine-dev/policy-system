"""The Authentik public path list is defined once, in the chart, and every consumer reads it."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = REPO_ROOT / "charts" / "policy-system"
PATHS_FILE_RELATIVE = "files/authentik-public-paths.txt"
PATHS_FILE = CHART_DIR / PATHS_FILE_RELATIVE
PROD_SCRIPT = REPO_ROOT / "scripts" / "deploy-ps-prod.sh"
EVAL_SCRIPT = REPO_ROOT / "scripts" / "deploy-ps-eval.sh"


def _defined_paths() -> list[str]:
    lines = (line.strip() for line in PATHS_FILE.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def test_the_shared_file_lists_the_paths() -> None:
    assert len(_defined_paths()) >= 1


def test_the_authentik_ingress_template_reads_the_shared_file() -> None:
    template = (CHART_DIR / "templates" / "ingress-authentik.yaml").read_text(encoding="utf-8")
    assert f'"file" "{PATHS_FILE_RELATIVE}"' in template


def test_the_production_script_reads_the_shared_file() -> None:
    assert PATHS_FILE_RELATIVE in PROD_SCRIPT.read_text(encoding="utf-8")


def test_no_other_file_repeats_a_path_from_the_shared_file() -> None:
    sources = [
        *sorted((CHART_DIR / "templates").glob("*")),
        PROD_SCRIPT,
        EVAL_SCRIPT,
        *sorted((REPO_ROOT / "scripts" / "lib").glob("*.sh")),
    ]
    for source in sources:
        lines = {line.strip().strip("'\"") for line in source.read_text().splitlines()}
        for path in _defined_paths():
            assert path not in lines, f"{source.name} repeats {path}"
