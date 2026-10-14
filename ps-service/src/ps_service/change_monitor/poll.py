"""`poll_for_amendments` -- detect newer consolidated versions of tracked instruments.

Enumerates the tracked instrument set from `policy_system` (via
`graph_reader.read_tracked_instruments`) and, one instrument at a time, asks
CELLAR whether it has published a consolidated expression newer than the
instrument's ingested baseline. The CELLAR lookup is injected
(`ConsolidatedVersionLookup`), defaulting to the real
`cellar_consolidated.fetch_consolidated_versions`.

Read-only: this function issues no graph write. It returns a complete
`PollReport` even when some instruments fail -- a per-instrument
`CellarConsolidationQueryError` is isolated (`outcome="poll_failed"`,
recorded in `failed_ids`) and the poll continues; a node with no
`celex` is `outcome="not_configured"` (recorded in `unconfigured_ids`), a
data gap rather than a transient failure. No other exception is swallowed.

`regulation` and `directive` take the identical path -- nothing in this
module branches on `TrackedInstrumentNode.instrument_type`'s value. The value
is carried into the finding untouched.

Baseline resolution, in priority order:

1. A caller-supplied override CELEX for this instrument id -- compared
   lexically against the latest consolidated CELEX.
2. Else the node's own `effective_date` -- compared against the latest
   consolidation's incorporation date.
3. Else (no override, no parseable `effective_date`): `amendment_detected`
   with `reason="baseline_unknown"` -- conservative, never a silent
   `current`.

Limitation: the poll never writes a baseline, so after a successful
`trigger_reingestion` it keeps reporting the same instrument as
`amendment_detected` until the caller passes an updated `baseline_overrides`
entry or the instrument's `effective_date` moves past the detected
consolidation.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Literal, Protocol

from ps_service.change_monitor.cellar_consolidated import fetch_consolidated_versions
from ps_service.change_monitor.errors import CellarConsolidationQueryError
from ps_service.change_monitor.graph_reader import read_tracked_instruments
from ps_service.change_monitor.models import AmendmentFinding, PollReport
from ps_service.logging import bind_run_context, emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ps_service.change_monitor.cellar_consolidated import ConsolidatedVersionInfo
    from ps_service.change_monitor.falkordb_client import GraphHandle
    from ps_service.change_monitor.models import TrackedInstrumentNode
    from ps_service.logging import LogEmitter

_COMPONENT = "change_monitor"
_ACTION = "poll_for_amendments"

_Outcome = Literal["current", "amendment_detected", "poll_failed", "not_configured"]


class ConsolidatedVersionLookup(Protocol):
    """Injection seam: given a base-act CELEX, return CELLAR's consolidated-version state.

    The default implementation is
    `cellar_consolidated.fetch_consolidated_versions`; a test substitutes a
    fake without touching the network. An implementation signals a per-instrument
    query failure by raising `CellarConsolidationQueryError`, which the poll isolates.
    """

    def __call__(self, base_celex: str) -> ConsolidatedVersionInfo:
        """Return CELLAR's consolidated-expression state for `base_celex`.

        Args:
            base_celex: Base-act CELEX in the legislation sector (e.g. `32016R0679`).

        Returns:
            The `ConsolidatedVersionInfo`; its `consolidated_celexes` is empty when CELLAR
            has no consolidated version.

        Raises:
            CellarConsolidationQueryError: The query could not be completed.
        """
        ...


def poll_for_amendments(
    graph: GraphHandle,
    *,
    baseline_overrides: Mapping[str, str] | None = None,
    consolidated_versions: ConsolidatedVersionLookup = fetch_consolidated_versions,
    emitter: LogEmitter | None = None,
) -> PollReport:
    """Poll every tracked instrument for a newer consolidated version.

    Tells an operator which tracked instruments have a newer consolidated
    version in CELLAR than the one ingested, so they can trigger re-ingestion.
    A per-instrument CELLAR failure never aborts the poll.

    Args:
        graph: Handle onto the merged `policy_system` graph, used for one
            read-only enumeration of tracked instruments.
        baseline_overrides: Maps a `regulatory_instrument_id` to the
            consolidated CELEX the caller last ingested; takes priority over
            the node's `effective_date` as the comparison baseline. `None`
            or empty means no overrides.
        consolidated_versions: CELLAR lookup; defaults to the real SPARQL
            client, injectable for tests.
        emitter: Log emitter for the per-instrument entries; `None` uses the
            default emitter.

    Returns:
        A complete `PollReport`: `findings` holds only instruments with an
        amendment detected, `polled_count` the number enumerated,
        `failed_ids` those whose CELLAR query failed and `unconfigured_ids`
        those with no `celex` and no override. All empty when nothing is
        tracked.

    Raises:
        Exception: Any error other than `CellarConsolidationQueryError`
            raised by the graph read or the lookup propagates.

    Side Effects:
        Binds a fresh run id for the whole call and emits exactly one log
        entry per enumerated instrument (`component="change_monitor"`,
        `action="poll_for_amendments"`, `entity_id=<id>`, `outcome`).
        Issues no graph write.
    """
    overrides: Mapping[str, str] = baseline_overrides or {}
    findings: list[AmendmentFinding] = []
    failed_ids: list[str] = []
    unconfigured_ids: list[str] = []
    with bind_run_context():
        instruments = read_tracked_instruments(graph)
        for node in instruments:
            outcome, finding = _poll_one(node, overrides, consolidated_versions)
            if finding is not None:
                findings.append(finding)
            if outcome == "poll_failed":
                failed_ids.append(node.regulatory_instrument_id)
            elif outcome == "not_configured":
                unconfigured_ids.append(node.regulatory_instrument_id)
            emit_log_entry(
                component=_COMPONENT,
                action=_ACTION,
                entity_id=node.regulatory_instrument_id,
                outcome=outcome,
                emitter=emitter,
            )
    return PollReport(
        findings=tuple(findings),
        polled_count=len(instruments),
        failed_ids=tuple(failed_ids),
        unconfigured_ids=tuple(unconfigured_ids),
    )


def _poll_one(
    node: TrackedInstrumentNode,
    overrides: Mapping[str, str],
    consolidated_versions: ConsolidatedVersionLookup,
) -> tuple[_Outcome, AmendmentFinding | None]:
    """Classify one instrument, isolating a CELLAR query failure as `poll_failed`."""
    if node.celex is None:
        return "not_configured", None
    try:
        info = consolidated_versions(node.celex)
    except CellarConsolidationQueryError:
        return "poll_failed", None
    return _classify(node, info, overrides)


def _classify(
    node: TrackedInstrumentNode,
    info: ConsolidatedVersionInfo,
    overrides: Mapping[str, str],
) -> tuple[_Outcome, AmendmentFinding | None]:
    """Compare CELLAR's latest consolidation against the resolved baseline."""
    latest = info.latest_celex
    latest_date = info.latest_consolidation_date
    if latest is None or latest_date is None:
        return "current", None
    override = overrides.get(node.regulatory_instrument_id)
    if override is not None:
        return _verdict(node, latest, latest_date, override, newer=latest > override)
    baseline = _parse_iso_date(node.effective_date)
    if baseline is None:
        return "amendment_detected", _finding(
            node, latest, latest_date, "unknown", "baseline_unknown"
        )
    return _verdict(node, latest, latest_date, node.effective_date, newer=latest_date > baseline)


def _verdict(
    node: TrackedInstrumentNode,
    latest_celex: str,
    latest_date: date,
    baseline_reference: str,
    *,
    newer: bool,
) -> tuple[_Outcome, AmendmentFinding | None]:
    """`amendment_detected` + a finding when `newer`, else `current`."""
    if not newer:
        return "current", None
    return "amendment_detected", _finding(
        node, latest_celex, latest_date, baseline_reference, "newer_consolidation"
    )


def _finding(
    node: TrackedInstrumentNode,
    latest_celex: str,
    latest_date: date,
    baseline_reference: str,
    reason: Literal["newer_consolidation", "baseline_unknown"],
) -> AmendmentFinding:
    """Build the `AmendmentFinding` for a detected amendment."""
    return AmendmentFinding(
        regulatory_instrument_id=node.regulatory_instrument_id,
        instrument_type=node.instrument_type,
        baseline_reference=baseline_reference,
        detected_consolidated_celex=latest_celex,
        detected_consolidation_date=latest_date,
        reason=reason,
    )


def _parse_iso_date(value: str) -> date | None:
    """Parse an ISO-8601 date string, or `None` when it is empty or unparseable."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
