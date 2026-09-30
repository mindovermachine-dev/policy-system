"""Static guard (issue #165 AC-BI-005): the chart ships no token literal and prints none.

The shared Authentik/PS Service API token is generated at install time
(`templates/authentik-api-token-secret.yaml`). No values file may carry a token value, and
the chart must not ship a NOTES.txt that could print one. Pure file reads, hermetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CHART_DIR = Path(__file__).resolve().parents[3] / "charts" / "policy-system"
RETIRED_PLACEHOLDER = "ps-service-authentik-dev-token"
VALUES_FILES = ("values.yaml", "values-prod.yaml")


def _load(name: str) -> dict[str, Any]:
    with (CHART_DIR / name).open(encoding="utf-8") as f:
        loaded: dict[str, Any] = yaml.safe_load(f)
    return loaded


def test_no_values_file_contains_the_retired_token_placeholder() -> None:
    for name in VALUES_FILES:
        text = (CHART_DIR / name).read_text(encoding="utf-8")
        assert RETIRED_PLACEHOLDER not in text, f"{name} still carries the placeholder token"


def test_ps_service_authentik_has_no_plain_api_token_value() -> None:
    for name in VALUES_FILES:
        authentik = _load(name).get("psService", {}).get("authentik", {})
        assert "apiToken" not in authentik, (
            f"{name}: psService.authentik.apiToken must not exist; the token is generated"
        )


def test_chart_ships_no_notes_template_that_could_print_the_token() -> None:
    assert not (CHART_DIR / "templates" / "NOTES.txt").exists()
