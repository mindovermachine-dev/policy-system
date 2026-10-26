"""Content lint: the operations guide says re-running a deploy script from a newer bundle upgrades.

The deploy scripts compare the bundle's chart version with the deployed one, so the guide must not
claim they skip a newer chart version, and must say a newer bundle upgrades the release.
"""

from __future__ import annotations

from pathlib import Path

_OPS = Path(__file__).resolve().parents[3] / "docs" / "artifacts" / "operations-guide.md"


def _guide() -> str:
    return " ".join(_OPS.read_text(encoding="utf-8").split())


def test_operations_guide_does_not_say_a_deploy_script_skips_a_newer_chart_version() -> None:
    guide = _guide()

    assert "does not pick up a newer chart version" not in guide
    assert "will **not** pick up a new chart version" not in guide


def test_operations_guide_says_a_newer_bundle_upgrades_the_release_for_both_scripts() -> None:
    guide = _guide()

    assert "`scripts/deploy-ps-eval.sh` from a newer bundle also upgrades the release" in guide
    assert "`scripts/deploy-ps-prod.sh` from a newer bundle therefore upgrades the release" in guide
