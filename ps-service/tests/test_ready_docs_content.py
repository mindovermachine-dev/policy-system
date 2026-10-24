"""Content lint: the architecture and operations docs describe the `/ready` re-probe.

`/ready` re-probes a gating dependency that reads unhealthy, so a mid-run outage ends on its own.
These tests pin the facts a reader needs (recovery without a restart, the two settings) and that
the wording and symbols of the earlier behaviour are gone from the code and both docs. Anchors are
headings and quoted text, never line numbers.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ARCH = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_OPS = _REPO / "docs" / "artifacts" / "operations-guide.md"
_VALREF = _REPO / "docs" / "artifacts" / "helm-chart-values-reference.md"
_MAIN = _REPO / "ps-service" / "src" / "ps_service" / "main.py"
_TEST_MAIN = _REPO / "ps-service" / "tests" / "test_main.py"
_SETTINGS = (
    "PS_SERVICE_READY_REPROBE_MIN_INTERVAL_SECONDS",
    "PS_SERVICE_READY_REPROBE_DEADLINE_SECONDS",
)
_SUPERSEDED = (
    re.compile(r"does not reset it"),
    re.compile(r"app\.state\.ready. is still", re.IGNORECASE),
    re.compile(
        r"while it is .False.|only while the startup check has not succeeded"
        r"|until a later call to that dependency succeeds",
        re.IGNORECASE,
    ),
)


def _bullet_starting(text: str, start: str) -> str:
    """Return the single line of `text` that begins with `start`."""
    matches = [line for line in text.splitlines() if line.startswith(start)]
    assert len(matches) == 1, f"expected one line starting with {start!r}, found {len(matches)}"
    return matches[0]


def test_architecture_checkreadiness_states_the_reprobe_and_both_settings() -> None:
    bullet = _bullet_starting(
        _ARCH.read_text(encoding="utf-8"), "- `CheckReadiness` (`/ready`) answers ready"
    )

    assert "recovers on its own" in bullet
    assert "no restart" in bullet
    for setting in _SETTINGS:
        assert setting in bullet


def test_operations_guide_states_ready_recovers_on_its_own_and_lists_both_settings() -> None:
    ops = _OPS.read_text(encoding="utf-8")
    row = _bullet_starting(ops, "| `/ready` answers 503 after FalkorDB or PS Postgres failed")

    assert "on its own" in row
    assert "no restart is needed" in row
    for setting in _SETTINGS:
        assert f"| `{setting}` |" in ops
    assert "Keep the deadline below the readiness\nprobe `timeoutSeconds`" in ops
    assert "PS_SERVICE_READY_REPROBE_DEADLINE_SECONDS" in _VALREF.read_text(encoding="utf-8")


def test_superseded_wording_is_gone_from_main_and_both_docs() -> None:
    for path in (_MAIN, _ARCH, _OPS):
        text = path.read_text(encoding="utf-8")
        for pattern in _SUPERSEDED:
            assert pattern.search(text) is None, f"{path.name}: {pattern.pattern}"
    row = _bullet_starting(
        _OPS.read_text(encoding="utf-8"),
        "| `/ready` answers 503 after FalkorDB or PS Postgres failed",
    )
    assert "Restart the `ps-service` pod" not in row


def test_removed_retry_symbols_are_gone_from_the_service_and_docs() -> None:
    names = ("_retry_gating_dependencies", "_graph_replay_probe", "ready_retry")
    roots = (_REPO / "ps-service", _REPO / "docs" / "architecture", _REPO / "docs" / "artifacts")
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".md"} or ".venv" in path.parts:
                continue
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8")
            for name in names:
                assert name not in text, f"{path}: {name}"


def test_the_mid_run_recovery_test_never_marks_the_dependency_healthy_itself() -> None:
    tree = ast.parse(_TEST_MAIN.read_text(encoding="utf-8"))
    recovery = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_ready_recovers_through_the_real_probe_after_a_mid_run_outage"
    )
    called = {
        call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
        for call in ast.walk(recovery)
        if isinstance(call, ast.Call)
    }

    assert "mark_healthy" not in called
