"""The evaluator reverse proxy is gone: the chart Ingress is the only way in.

Reads the real tracked files off disk; no fakes. The chart has no proxy templates, tests or values,
the footprint script and its dedicated deploy tests are removed, and nothing tracked still refers to
a switch that turned the proxy on or off.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]
_CHART = _REPO / "charts" / "policy-system"
_SWITCH = "proxy" + ".enabled"
_EXCLUDED_PREFIXES = (".orchestrator/", "spikes/")


def _tracked_files() -> list[Path]:
    git = shutil.which("git")
    assert git is not None
    listing = subprocess.run(  # noqa: S603 - absolute git path, literal arguments
        [
            git,
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=_REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [
        _REPO / name
        for name in listing.split("\0")
        if name and not name.startswith(_EXCLUDED_PREFIXES) and (_REPO / name).is_file()
    ]


def test_no_proxy_templates_remain_in_the_chart() -> None:
    assert sorted(p.name for p in (_CHART / "templates").glob("proxy-*.yaml")) == []


def test_no_proxy_chart_unit_tests_remain() -> None:
    assert sorted(p.name for p in (_CHART / "tests").glob("proxy-*_test.yaml")) == []


def test_no_proxy_helpers_remain_in_the_chart() -> None:
    helpers = (_CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    assert "policy-system.proxy" not in helpers


def test_footprint_script_and_dedicated_deploy_tests_are_removed() -> None:
    assert not (_REPO / "scripts" / "measure-proxy-footprint.sh").exists()
    assert not (_REPO / "ps-service" / "tests" / "deploy_proxy").exists()


def test_values_have_no_top_level_proxy_key() -> None:
    values = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))
    assert "proxy" not in values


def test_no_tracked_file_mentions_the_proxy_switch() -> None:
    this_file = Path(__file__).resolve()
    offenders: list[str] = []
    for path in _tracked_files():
        if path.resolve() == this_file:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _SWITCH in text:
            offenders.append(str(path.relative_to(_REPO)))
    assert offenders == []
