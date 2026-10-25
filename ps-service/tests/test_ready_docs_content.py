"""Content lint: the architecture and operations docs describe the probes and `/status`.

`/readyz` re-probes a gating dependency that reads unhealthy, so a mid-run outage ends on its own.
These tests pin the facts a reader needs (recovery without a restart, the two settings), that
the wording and symbols of the earlier behaviour are gone from the code and both docs, that the
three architecture docs name `/status` as the only client status surface and the probes as
in-cluster only, and that the operations guide's upgrade note says what moves together. Anchors
are headings, quoted text and HTML comment markers, never line numbers.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ARCH = _REPO / "docs" / "architecture" / "ps-service-container-architecture.md"
_SOLUTION = _REPO / "docs" / "architecture" / "ps-solution-architecture.md"
_CLI_ARCH = _REPO / "docs" / "architecture" / "ps-cli-container-architecture.md"
_OPS = _REPO / "docs" / "artifacts" / "operations-guide.md"
_VALREF = _REPO / "docs" / "artifacts" / "helm-chart-values-reference.md"
_MAIN = _REPO / "ps-service" / "src" / "ps_service" / "main.py"
_TEST_MAIN = _REPO / "ps-service" / "tests" / "test_main.py"
_SETTINGS = (
    "PS_SERVICE_READY_REPROBE_MIN_INTERVAL_SECONDS",
    "PS_SERVICE_READY_REPROBE_DEADLINE_SECONDS",
)
_RETIRED_PATHS = tuple(f"/{name}" for name in ("health", "ready"))
_UPGRADE_NOTE_START = "<!-- upgrade-note:probe-rename:start -->"
_UPGRADE_NOTE_END = "<!-- upgrade-note:probe-rename:end -->"
_SUPERSEDED = (
    re.compile(r"does not reset it"),
    re.compile(r"app\.state\.ready. is still", re.IGNORECASE),
    re.compile(
        r"while it is .False.|only while the startup check has not succeeded"
        r"|until a later call to that dependency succeeds",
        re.IGNORECASE,
    ),
)


def _collapsed(text: str) -> str:
    """Return `text` with every run of whitespace replaced by one space."""
    return " ".join(text.split())


def _upgrade_note(text: str) -> str:
    """Return the text between the probe-rename upgrade-note markers, each present exactly once."""
    for marker in (_UPGRADE_NOTE_START, _UPGRADE_NOTE_END):
        assert text.count(marker) == 1, f"expected {marker!r} exactly once"
    start = text.index(_UPGRADE_NOTE_START) + len(_UPGRADE_NOTE_START)
    end = text.index(_UPGRADE_NOTE_END)
    assert start < end, "the upgrade-note start marker must come before its end marker"
    return text[start:end]


def _bullet_starting(text: str, start: str) -> str:
    """Return the single line of `text` that begins with `start`."""
    matches = [line for line in text.splitlines() if line.startswith(start)]
    assert len(matches) == 1, f"expected one line starting with {start!r}, found {len(matches)}"
    return matches[0]


def test_architecture_checkreadiness_states_the_reprobe_and_both_settings() -> None:
    bullet = _bullet_starting(
        _ARCH.read_text(encoding="utf-8"), "- `CheckReadiness` (`/readyz`) answers ready"
    )

    assert "recovers on its own" in bullet
    assert "no restart" in bullet
    for setting in _SETTINGS:
        assert setting in bullet


def test_operations_guide_states_ready_recovers_on_its_own_and_lists_both_settings() -> None:
    ops = _OPS.read_text(encoding="utf-8")
    row = _bullet_starting(ops, "| `/readyz` answers 503 after FalkorDB or PS Postgres failed")

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
        "| `/readyz` answers 503 after FalkorDB or PS Postgres failed",
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


def test_architecture_docs_name_status_as_the_only_client_status_surface() -> None:
    for path in (_ARCH, _SOLUTION, _CLI_ARCH):
        text = _collapsed(path.read_text(encoding="utf-8"))
        assert "`/status` is the only client-facing status surface" in text, path.name
        assert "`/livez` and `/readyz` are orchestrator-only and in-cluster" in text, path.name


def test_operations_guide_upgrade_note_says_what_moves_together_and_where_clients_go() -> None:
    note = _collapsed(_upgrade_note(_OPS.read_text(encoding="utf-8")))
    old_liveness, old_readiness = _RETIRED_PATHS

    for needle in (
        "### Upgrading to the client status endpoint",
        "image, chart and ps-cli must be upgraded together",
        (
            "a chart from before this release probes paths the new image no longer serves, "
            "so its pods fail their probes"
        ),
        "external monitors",
        "hand reads of the dependency detail",
        "move to `/status`",
        "`/livez` and `/readyz` are no longer reachable through the Ingress",
        f"`{old_liveness}` is now `/livez`",
        f"`{old_readiness}` is now `/readyz`",
        "the liveness body no longer carries `version`",
        "the readiness body no longer carries the dependency detail",
    ):
        assert needle in note, needle


def test_operations_guide_links_the_upgrade_note_outside_the_block() -> None:
    ops = _OPS.read_text(encoding="utf-8")
    note = _upgrade_note(ops)
    outside = ops.replace(note, "")

    assert "(#upgrading-to-the-client-status-endpoint)" in outside
