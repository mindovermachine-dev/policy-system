"""The chart's defaults agree with the service's compiled defaults and with the deploy script.

The chart renders every startup setting below into the PS Service environment, so a default that
drifted from the one compiled into `ServiceConfig` would silently change behaviour for operators
who leave the value alone. The startup probe window must also cover the discovery retry budget and
still fit inside the wait the deploy script gives `helm upgrade`.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import cast

import yaml

from ps_service.config import ServiceConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
VALUES_FILE = REPO_ROOT / "charts" / "policy-system" / "values.yaml"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy-ps-prod.sh"

# Process boot plus one discovery attempt that starts just before the budget ends (30 s timeout).
_STARTUP_MARGIN_SECONDS = 90


def _ps_service_values() -> dict[str, object]:
    values = cast("dict[str, object]", yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8")))
    return cast("dict[str, object]", values["psService"])


def _compiled_defaults() -> ServiceConfig:
    return ServiceConfig(
        host="127.0.0.1", port=8000, graceful_shutdown_seconds=10, logging_dir=None
    )


def _helm_wait_seconds() -> int:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"helm upgrade --install.*?--timeout (\d+)m", script, re.DOTALL)
    assert match is not None, "the helm upgrade --install timeout is not found in the script"
    return int(match.group(1)) * 60


def _chart_budget() -> int:
    auth = cast("dict[str, object]", _ps_service_values()["auth"])
    return cast("int", auth["discoveryRetryBudgetSeconds"])


def test_chart_default_max_request_body_bytes_matches_compiled_default() -> None:
    assert (
        _ps_service_values()["maxRequestBodyBytes"] == _compiled_defaults().max_request_body_bytes
    )


def test_chart_default_discovery_budget_matches_compiled_default() -> None:
    assert _chart_budget() == _compiled_defaults().auth_discovery_retry_budget_seconds


def test_default_budget_is_shorter_than_the_helm_wait_timeout() -> None:
    assert _chart_budget() < _helm_wait_seconds()


def test_startup_window_covers_the_retry_budget_and_fits_the_helm_timeout() -> None:
    probes = cast("dict[str, dict[str, int]]", _ps_service_values()["probes"])
    period = probes["startup"]["periodSeconds"]
    budget = _chart_budget()

    threshold = math.ceil((budget + _STARTUP_MARGIN_SECONDS) / period)
    window = threshold * period

    assert window >= budget + _STARTUP_MARGIN_SECONDS
    assert window < _helm_wait_seconds()
