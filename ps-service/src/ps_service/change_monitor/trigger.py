"""`trigger_reingestion` -- the UC-4 re-entry point (AC-004..AC-009).

Given a base-act `identifier`, its `short_name`, and the `new_version` to
record, re-ingest the instrument's structure into its `{short_name}_native`
graph and record the `SUPERSEDED_BY` succession from the prior active
version to the new one.

Decomposition (PLAN_REVIEWED.md §1.4, flaw 9): `_preflight` is a pure,
read-only classification returning a small `_Preflight` result object;
`trigger_reingestion`'s body is a flat orchestration over that object with
no nested conditionals (complexity <= 8).

`_preflight` classifies all three states (`fresh` / `resume` /
`already_processed`); the body is a flat orchestration over them.

The `national_transposition` guard (AC-010, `_guard_national_transposition`)
runs after the `already_processed` short-circuit and before any write.

`fresh` ordering (AC-006/007): `_preflight` -> guard ->
`ingest_regulatory_instrument` -> `set_new_version_property` -> the single
fused `link_and_supersede` -> emit one `link_superseded_by` entry. No
try/except around the ingest call -- a failure propagates unchanged with
nothing written (AC-007), mirroring `ingestion.pipeline`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ps_service.change_monitor.errors import (
    ChangeMonitorStateError,
    NationalTranspositionNotSupportedError,
)
from ps_service.change_monitor.models import ReingestionOutcome
from ps_service.change_monitor.succession import (
    find_prior_instrument,
    is_succession_complete,
    link_and_supersede,
    new_node_exists,
    set_new_version_property,
)
from ps_service.ingestion.pipeline import ingest_regulatory_instrument
from ps_service.logging import emit_log_entry

if TYPE_CHECKING:
    from ps_service.change_monitor.falkordb_client import GraphHandle
    from ps_service.ingestion.adapters.base import IngestionAdapter
    from ps_service.logging import LogEmitter

_COMPONENT = "change_monitor"
_LINK_ACTION = "link_superseded_by"
_LINK_OUTCOME = "superseded"

# The one instrument type `trigger_reingestion` refuses (AC-010). It is an
# instrument-type token, not a regulation name or CELEX, so it is free of the
# AC-011 "no regulation literal in a conditional" constraint.
_NATIONAL_TRANSPOSITION = "national_transposition"


@dataclass(frozen=True, slots=True)
class _Preflight:
    """The read-only classification of the graph state before any write.

    `prior_id` is the prior active instrument's id for `fresh` / `resume`,
    and the completed-edge prior's id for `already_processed`.
    `prior_instrument_type` is carried for Increment 10a's AC-010 guard and
    is `None` on the `already_processed` path (read off the completed edge,
    no prior lookup performed).
    """

    state: Literal["fresh", "resume", "already_processed"]
    prior_id: str | None
    prior_instrument_type: str | None


def _preflight(graph: GraphHandle, new_id: str) -> _Preflight:
    """Classify the graph state for `new_id` without mutating anything.

    `already_processed` when the completed-succession probe finds a
    `superseded` prior already linked into `new_id`; otherwise `resume` when
    the `new_id` node exists (a crash between ingest and succession) and
    `fresh` when it does not. `fresh` / `resume` both require exactly one
    active prior -- `find_prior_instrument` raises `ChangeMonitorStateError`
    otherwise.

    (PLAN_REVIEWED.md §1.4 names this `_preflight(graph, short_name, new_id)`;
    `short_name` is dropped here -- no probe in the decision table uses it,
    and `find_prior_instrument`'s §2 signature takes `new_id` only, so a
    `short_name` parameter would be unused.)
    """
    completed_prior = is_succession_complete(graph, new_id)
    if completed_prior is not None:
        return _Preflight(
            state="already_processed", prior_id=completed_prior, prior_instrument_type=None
        )
    status = new_node_exists(graph, new_id)
    prior = find_prior_instrument(graph, new_id)
    state: Literal["fresh", "resume"] = "resume" if status is not None else "fresh"
    return _Preflight(state=state, prior_id=prior.id, prior_instrument_type=prior.instrument_type)


def _guard_national_transposition(
    preflight: _Preflight, *, adapter: IngestionAdapter, identifier: str
) -> None:
    """Reject a `national_transposition` instrument before any ingest or write (AC-010).

    Two limbs (PLAN_REVIEWED.md §1.4, flaws 10 + 11):

    1. The prior node's `instrument_type`, carried through `_preflight`. This
       is the *only* limb that can actually fire for `CellarEliAdapter`
       today -- the real AC-010 guard.
    2. Belt-and-braces: one `fetch_regulatory_instrument_structure` call,
       rejecting the fetched metadata's `instrument_type`. This limb is
       *untested-by-construction for `CellarEliAdapter`* -- its type-code map
       is `{R: regulation, L: directive}` and it raises `CellarParseError`
       for anything else, so a `national_transposition` structure can never
       come back. Kept as a forward-looking defence at the cost of one extra
       fetch+parse (the pipeline fetches again -- Follow-on B removes it).

    Only the `== national_transposition` comparison is made here -- there is
    deliberately no `regulation` vs `directive` branch (AC-010/AC-011): the
    two framework types take the identical path.
    """
    if preflight.prior_instrument_type == _NATIONAL_TRANSPOSITION:
        raise NationalTranspositionNotSupportedError
    structure = adapter.fetch_regulatory_instrument_structure(identifier)
    if structure.metadata.instrument_type == _NATIONAL_TRANSPOSITION:
        raise NationalTranspositionNotSupportedError


def trigger_reingestion(
    identifier: str,
    short_name: str,
    new_version: str,
    *,
    adapter: IngestionAdapter,
    graph: GraphHandle,
    emitter: LogEmitter | None = None,
) -> ReingestionOutcome:
    """Re-ingest `identifier` as `new_version` and record its succession.

    `graph` is the `{short_name}_native` handle -- where the re-ingest and
    every succession write land; `trigger_reingestion` never touches
    `policy_system`. `identifier` is whatever the injected `adapter` expects
    (a base-act CELEX for `CellarEliAdapter`).

    `fresh` path: guard against `national_transposition`, re-ingest, write
    `new_version` onto the new node, fuse the `SUPERSEDED_BY` edge with
    `prior.status='superseded'`, emit one `link_superseded_by` log entry
    carrying the re-ingest's `run_id`, and return `outcome="superseded"`.

    `resume` (a crash between ingest and succession): skip the re-ingest,
    run only the idempotent fused write, emit one entry with `run_id=None`,
    return `outcome="superseded"`.

    `already_processed`: a no-op returning `run_id=None` and emitting nothing
    (a repeat call is not a new supersession event).

    Raises `NationalTranspositionNotSupportedError` (AC-010, before any
    write) or `ChangeMonitorStateError` when the graph has no single active
    prior.
    """
    new_id = f"{short_name}-{new_version}"
    preflight = _preflight(graph, new_id)

    if preflight.state == "already_processed":
        return ReingestionOutcome(
            prior_regulatory_instrument_id=preflight.prior_id or "",
            new_regulatory_instrument_id=new_id,
            run_id=None,
            outcome="already_processed",
            ingest_counts=None,
        )

    _guard_national_transposition(preflight, adapter=adapter, identifier=identifier)

    prior_id = preflight.prior_id
    if prior_id is None:  # unreachable: _preflight sets prior_id for fresh / resume
        raise ChangeMonitorStateError(
            f"preflight state {preflight.state!r} carried no prior id for {new_id!r}"
        )

    if preflight.state == "resume":
        link_and_supersede(graph, prior_id, new_id)
        _emit_link(prior_id, new_id, run_id=None, emitter=emitter)
        return ReingestionOutcome(
            prior_regulatory_instrument_id=prior_id,
            new_regulatory_instrument_id=new_id,
            run_id=None,
            outcome="superseded",
            ingest_counts=None,
        )

    result = ingest_regulatory_instrument(
        identifier,
        short_name,
        version=new_version,
        adapter=adapter,
        graph=graph,
        emitter=emitter,
    )
    set_new_version_property(graph, new_id, new_version)
    link_and_supersede(graph, prior_id, new_id)
    _emit_link(prior_id, new_id, run_id=result.run_id, emitter=emitter)
    return ReingestionOutcome(
        prior_regulatory_instrument_id=prior_id,
        new_regulatory_instrument_id=new_id,
        run_id=result.run_id,
        outcome="superseded",
        ingest_counts=result.counts,
    )


def _emit_link(
    prior_id: str, new_id: str, *, run_id: str | None, emitter: LogEmitter | None
) -> None:
    """Emit the single `link_superseded_by` entry after the fused write succeeds.

    `entity_id` is the `(prior_id, new_id)` tuple -- `LogEntry.to_json_line`
    serialises it as a 2-element JSON array, the carrier for AC-009's "old +
    new regulatory_instrument_id on one entry". `run_id` is passed
    explicitly on the `fresh` path (the ingest's `bind_run_context` has
    already exited) and is `None` on `resume`.
    """
    emit_log_entry(
        component=_COMPONENT,
        action=_LINK_ACTION,
        entity_id=(prior_id, new_id),
        outcome=_LINK_OUTCOME,
        run_id=run_id,
        emitter=emitter,
    )
