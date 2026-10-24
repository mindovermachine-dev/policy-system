"""Durable record of Company Merge's semantic Capability resolutions.

When dedup resolves an incoming Capability id `X` onto an existing Capability `A` by similarity,
`X` is never minted. A `ResolvedCapability {id: X, resolves_to: A}` marker records that decision
in the single-tenant graph, so a later ingest or restore that regenerates `X` follows the marker
instead of scoring `X` again against a candidate pool that has grown since. Only an explicit
Compliance Officer merge then changes where `X` ends up.

The marker is not a merge marker: no node was absorbed, there is no audit snapshot behind it and
nothing to unmerge. It is written as an `UpsertNode`, which merges by `(label, id)`, so writing the
same resolution again leaves one node.

The live merge and the offline restore both build and log the markers here, so they decide
identically. The caller puts the primitives in the same gateway group as the rewired edges, so a
marker and the `REQUIRES` edges onto its target are written together or not at all, and logs the
markers only once that group is applied.

A later run follows a marker through `redirects.resolve_redirects`, before dedup. The redirected
Capability ids of such a run, each with its terminal and the marker it was found under, are built
and logged here too, so both paths report them identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ps_service.company_merge.dedup import DEDUP_DECISION_ACTION
from ps_service.company_merge.redirects import compose_with_resolutions
from ps_service.graph_gateway.models import UpsertNode
from ps_service.logging import emit_log_entry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ps_service.company_merge.models import CanonicalResolution
    from ps_service.company_merge.redirects import RedirectResolution
    from ps_service.logging import LogEmitter

__all__ = [
    "RESOLVED_CAPABILITY_LABEL",
    "RESOLVES_TO",
    "CapabilityRedirect",
    "build_resolution_marker_primitives",
    "capability_redirects",
    "log_capability_redirects",
    "log_resolution_markers",
    "semantic_resolutions",
]

RESOLVED_CAPABILITY_LABEL = "ResolvedCapability"
RESOLVES_TO = "resolves_to"


@dataclass(frozen=True, slots=True)
class CapabilityRedirect:
    """One incoming Capability id a marker redirected before dedup.

    Attributes:
        incoming_id: The Capability id the baseline regenerated.
        terminal_id: The Capability its edges land on: the end of its marker chain, composed with
            this run's dedup resolutions.
        marker_label: The label of the marker the incoming id was found under
            (`ResolvedCapability` or `MergedCapability`).
    """

    incoming_id: str
    terminal_id: str
    marker_label: str


def semantic_resolutions(
    resolutions: Sequence[CanonicalResolution],
) -> tuple[CanonicalResolution, ...]:
    """Return the resolutions that matched an existing Capability by similarity, in input order.

    Args:
        resolutions: One dedup pass's Capability resolutions.

    Returns:
        Those with `match_kind == "semantic"`; an exact or new resolution records nothing.
    """
    return tuple(r for r in resolutions if r.match_kind == "semantic")


def build_resolution_marker_primitives(
    resolutions: Sequence[CanonicalResolution],
) -> tuple[UpsertNode, ...]:
    """Build one `ResolvedCapability` upsert per semantic resolution.

    Args:
        resolutions: One dedup pass's Capability resolutions.

    Returns:
        `UpsertNode(label="ResolvedCapability", id=incoming_id,
        properties={"resolves_to": canonical_id})` per semantic resolution, in input order; empty
        when none is semantic.
    """
    return tuple(
        UpsertNode(
            label=RESOLVED_CAPABILITY_LABEL,
            id=resolution.incoming_id,
            properties={RESOLVES_TO: resolution.canonical_id},
        )
        for resolution in semantic_resolutions(resolutions)
    )


def log_resolution_markers(
    resolutions: Sequence[CanonicalResolution],
    *,
    regulatory_instrument_id: str,
    emitter: LogEmitter | None = None,
) -> None:
    """Emit one `record_semantic_resolution` entry per marker written.

    Call only once the group carrying the markers is applied, so an entry means "written".

    Args:
        resolutions: The resolutions the markers were built from; only semantic ones are logged.
        regulatory_instrument_id: The run's instrument.
        emitter: Log sink; None uses the default emitter.

    Side Effects:
        One log entry per semantic resolution: `entity_id` is the incoming id, and `extra` carries
        the kind, the `resolves_to` id, the similarity and the instrument id. No text content.
    """
    for resolution in semantic_resolutions(resolutions):
        emit_log_entry(
            component="company_merge",
            action="record_semantic_resolution",
            entity_id=resolution.incoming_id,
            outcome="succeeded",
            extra={
                "kind": "Capability",
                RESOLVES_TO: resolution.canonical_id,
                "similarity": resolution.similarity,
                "regulatory_instrument_id": regulatory_instrument_id,
            },
            emitter=emitter,
        )


def capability_redirects(
    redirect: RedirectResolution, resolutions: Sequence[CanonicalResolution]
) -> tuple[CapabilityRedirect, ...]:
    """Build one `CapabilityRedirect` per Capability id the markers redirected.

    Args:
        redirect: The run's `resolve_redirects` outcome.
        resolutions: The run's Capability dedup resolutions, which may move a terminal that is
            itself a baseline Capability onto its canonical id.

    Returns:
        One entry per redirected Capability id, in the order `resolve_redirects` found them.
    """
    terminals = compose_with_resolutions(redirect.by_kind["Capability"], resolutions)
    return tuple(
        CapabilityRedirect(
            incoming_id=incoming_id,
            terminal_id=terminal_id,
            marker_label=redirect.first_hop[incoming_id],
        )
        for incoming_id, terminal_id in terminals.items()
    )


def log_capability_redirects(
    redirects: Sequence[CapabilityRedirect],
    *,
    regulatory_instrument_id: str,
    emitter: LogEmitter | None = None,
) -> None:
    """Emit one `dedupe_canonical_nodes` entry with `outcome="redirected"` per redirected id.

    A redirected id never reaches dedup, so this entry is the run's only decision entry for it.

    Args:
        redirects: The run's redirected Capability ids.
        regulatory_instrument_id: The run's instrument.
        emitter: Log sink; None uses the default emitter.

    Side Effects:
        One log entry per redirect: `entity_id` is the incoming id, and `extra` carries the kind,
        the terminal id, the label of the first marker followed and the instrument id. No text
        content.
    """
    for redirect in redirects:
        emit_log_entry(
            component="company_merge",
            action=DEDUP_DECISION_ACTION,
            entity_id=redirect.incoming_id,
            outcome="redirected",
            extra={
                "kind": "Capability",
                "terminal_id": redirect.terminal_id,
                "marker": redirect.marker_label,
                "regulatory_instrument_id": regulatory_instrument_id,
            },
            emitter=emitter,
        )
