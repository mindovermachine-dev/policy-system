"""LLM prompt strings + response-parsing helpers for `ps_service.domain_mapper`.

This module holds the extraction stage (PLAN_REVIEWED.md §11 Increment 6)
and the obligation-derivation stage (Increment 11). Capability-derivation
prompts/parsers are added by a later increment (14) in the same module.

Extraction system prompt is ported in spirit from
`spikes/cellar2/extract_requirements.py::_SYSTEM_PROMPT`, retaining its two
LEARNINGS.md-proven exclusions verbatim in effect (not copied word-for-word,
re-composed against this component's own `RequirementCandidate` field
names):

1. Scope/applicability clauses are not duties.
2. Conditional-permissive "may be X where <conditions>" constructions are
   not duties.

Per PLAN_REVIEWED.md §5.3/Open Question 8, the model is instructed not to
emit a candidate at all for these two categories, rather than emit one with
a deliberately low `confidence`. This is a deliberate, permanent design
tradeoff (not a gap this issue's implementation can or should try to close)
— see PLAN_REVIEWED.md §13 Open Question 8 for the full reasoning. Do not
"fix" this by changing the prompt to always extract and rely on confidence
alone; that is an explicitly out-of-scope behavior change.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from ps_service.domain_mapper.errors import (
    DomainMapperDerivationError,
    DomainMapperExtractionError,
)
from ps_service.domain_mapper.identity import capability_id, obligation_id
from ps_service.domain_mapper.models import (
    CapabilityDecision,
    ExtractionUnit,
    ObligationAssignment,
    RequirementCandidate,
)

EXTRACTION_SYSTEM_PROMPT = """You extract operative regulatory duties from one paragraph (or \
single-block article) of an EU regulation/directive, for a compliance graph.

For each independent duty in the given text, extract a requirement:
- type "requirement": an operative "shall" duty.
- type "prohibition": an operative "shall not" duty.
- type "recommendation": a "should"-phrased recommendation.

Do NOT extract a candidate at all for:
- Permissive "may" text with no embedded conditional "shall", including a \
conditional-permissive construction like "may be limited/excluded/extended \
where/if <conditions>" -- this states a possibility gated on conditions, not \
an operative duty on any actor.
- Institutional/procedural text (delegated-act powers, committee mechanics, \
notification-to-Commission housekeeping with no substantive duty).
- A clause that is purely a cross-reference/citation with no operative \
content of its own.
- Applicability/scope-definition text (what the regulation covers, to which \
products or entities it applies, definitions of terms) -- this describes the \
regulation's own scope, not a duty imposed on any actor, even when phrased \
with "applies to" or "shall mean". A sentence whose grammatical subject is \
"This Regulation"/"This Directive" itself, not a real-world actor, is never a \
requirement.

role_name: the substantive duty-bearing actor -- the one the text actually \
assigns the duty to, in specific named-role terms (e.g. "Manufacturer", \
"Data Controller"), never a generic umbrella term like "economic operator" if \
a more specific role is discernible from context. If the text is phrased as a \
Directive-style wrapper ("Member States shall ensure that <entity> \
shall..."), attribute the duty to <entity>, not "Member States" -- Member \
States are not the substantive duty-bearer.

Granularity: one requirement per independently-required duty. If the \
paragraph bundles more than one independent duty with no existing letter to \
distinguish them, invent trailing letters "a", "b", "c"... in the order the \
duties appear, and set letter_suffix to that letter (null if the paragraph \
is not split). If the paragraph already contains a cumulative lettered \
checklist where each point is independently required, split each point into \
its own requirement. If instead it is a disjunctive list ("one of the \
following..."), keep it as ONE requirement -- splitting a disjunctive list \
would manufacture a false competing duty.

confidence: your own certainty, 0.0-1.0, that this is genuinely an operative \
requirement/prohibition/recommendation (not a borderline case). Always \
include it, for every requirement, unconditionally.

Return strict JSON with a top-level "requirements" key: {"requirements": \
[{"role_name": str, "text": str, "type": "requirement"|"prohibition"|\
"recommendation", "letter_suffix": str|null, "confidence": float}]}. Return \
{"requirements": []} if the text has no operative duty."""


def parse_extraction_response(text: str, unit: ExtractionUnit) -> list[RequirementCandidate]:
    """Parse one unit's raw LLM completion text into `RequirementCandidate`s.

    `unit_citation_ref`/`unit_article_number`/`unit_paragraph_number` are
    populated from `unit` (the source location the model was never asked to
    reproduce); every other field comes from the LLM's own JSON. Raises
    `DomainMapperExtractionError`, naming `unit.citation_ref`, on:
    - malformed JSON (`json.JSONDecodeError`),
    - a response missing the top-level `"requirements"` key,
    - any item that fails `RequirementCandidate` validation (e.g. a missing
      `confidence` — never silently defaulted).
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DomainMapperExtractionError(
            f"extraction response for {unit.citation_ref!r} was not valid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict) or "requirements" not in payload:
        raise DomainMapperExtractionError(
            f"extraction response for {unit.citation_ref!r} is missing the top-level "
            f"'requirements' key: {payload!r}"
        )

    items: Any = payload["requirements"]
    if not isinstance(items, list):
        raise DomainMapperExtractionError(
            f"extraction response for {unit.citation_ref!r} has a non-list 'requirements' "
            f"value: {items!r}"
        )

    return [_build_candidate(item, unit) for item in items]


def _build_candidate(item: Any, unit: ExtractionUnit) -> RequirementCandidate:
    """Validate one raw JSON item against `RequirementCandidate`, raising
    `DomainMapperExtractionError` (naming `unit.citation_ref`) on a
    `pydantic.ValidationError` — a missing/invalid `confidence` (or any
    other field) is a typed error, never a silent default."""
    if not isinstance(item, dict):
        raise DomainMapperExtractionError(
            f"extraction response for {unit.citation_ref!r} has a non-object requirement "
            f"item: {item!r}"
        )
    try:
        return RequirementCandidate.model_validate(
            {
                **item,
                "unit_citation_ref": unit.citation_ref,
                "unit_article_number": unit.article_number,
                "unit_paragraph_number": unit.paragraph_number,
            }
        )
    except ValidationError as exc:
        raise DomainMapperExtractionError(
            f"extraction response for {unit.citation_ref!r} had a malformed requirement "
            f"item: {exc}"
        ) from exc


# --- Obligation derivation (PLAN_REVIEWED.md §11 Increment 11) -------------
#
# Ported in spirit from `spikes/cellar2/derive_obligations.py::_SYSTEM_PROMPT`,
# extended with an explicit `unmatchable: bool` third outcome (§7.5's AC-004
# unmatched-handling mechanism) alongside the spike's original
# `matched_existing_id`/`new_text` pair. `role_view` (this Role's own slice
# of the whole-run registry, §7.3) is presented as the match-candidate set
# for the mint-or-match decision — the model is never shown the whole-run
# registry, only what has already been assigned to the current Role, which
# is deliberate: cross-Role collision detection is a code-level guarantee
# (`derivation.py::_resolve_obligation_id`, the B1 fix), never delegated to
# the model's own judgment.

OBLIGATION_DERIVATION_SYSTEM_PROMPT = """You maintain a canonical registry of Obligations \
(reusable duty statements) for one Role in a compliance graph. Given a Requirement's duty \
text and the Role's existing Obligation registry (already-registered duty statements for \
THIS role only), decide exactly one of three outcomes:

- MATCH: if this Requirement's duty is genuinely the same duty as an existing registry \
entry (it may be worded differently), return that entry's id in matched_existing_id.
- MINT: otherwise, if this is a genuine new duty for this Role, return a terse, generic \
duty statement in new_text (e.g. "Conduct Cybersecurity Risk Assessment", "Report Security \
Incidents") that could be reused if a similarly-worded duty appears again later for this \
Role.
- UNMATCHABLE: if the Requirement's text does not state a genuine, coherent duty that can \
be matched or minted as an Obligation at all (e.g. it is too vague, purely procedural, or \
incomplete to derive a duty statement from), set unmatchable to true instead.

confidence: your own certainty, 0.0-1.0, in this specific decision (matching, minting, or \
declaring unmatchable). Always include it, unconditionally.

Return strict JSON: {"matched_existing_id": str|null, "new_text": str|null, "unmatchable": \
bool, "confidence": float}. Exactly one of matched_existing_id/new_text must be non-null, \
UNLESS unmatchable is true, in which case both must be null."""


def parse_obligation_response(
    text: str,
    requirement_id: str,
    role_node_id: str,
    role_view_registry: dict[str, str],
) -> ObligationAssignment:
    """Parse one Requirement's raw obligation-derivation completion text
    into an `ObligationAssignment`.

    `role_view_registry` is the calling Role's own slice of the whole-run
    registry (`obligation_id -> text`, §7.3) — used to resolve a
    `matched_existing_id` back into its text (the model is never asked to
    repeat text it already has access to).

    Three valid outcomes, per `OBLIGATION_DERIVATION_SYSTEM_PROMPT`:
    - `unmatchable: true` -> `ObligationAssignment(obligation_node_id=None,
      obligation_text=None, ...)`. Not an error — a valid, expected AC-004
      outcome (§7.5).
    - a `matched_existing_id` present in `role_view_registry` -> the
      assignment's `obligation_text` is that registry entry's text, and
      `obligation_node_id` is `identity.obligation_id()` recomputed from
      it (always equal to `matched_existing_id` itself, since registry
      keys are themselves `obligation_id()` outputs).
    - a non-empty `new_text` -> the assignment's `obligation_text` is
      `new_text`, `obligation_node_id` is `identity.obligation_id(new_text)`.

    Raises `DomainMapperDerivationError`, naming `requirement_id`, on:
    - malformed JSON (`json.JSONDecodeError`) or a non-object response,
    - a response with neither a `matched_existing_id` that resolves within
      `role_view_registry`, nor a valid `new_text`, nor `unmatchable: true`
      set — the exact shape `spikes/cellar2/LEARNINGS.md`'s B1 finding
      documents ("neither a valid match nor a new-text value"),
    - an invalid/missing `confidence` (via `ObligationAssignment`'s own
      `Field` constraint, surfaced as this same typed error, not a bare
      `pydantic.ValidationError`).
    """
    payload = _load_obligation_payload(text, requirement_id)

    if payload.get("unmatchable") is True:
        return _build_obligation_assignment(
            requirement_id=requirement_id,
            role_node_id=role_node_id,
            obligation_node_id=None,
            obligation_text=None,
            confidence=payload.get("confidence"),
        )

    proposed_text = _resolve_proposed_obligation_text(payload, requirement_id, role_view_registry)
    return _build_obligation_assignment(
        requirement_id=requirement_id,
        role_node_id=role_node_id,
        obligation_node_id=obligation_id(proposed_text),
        obligation_text=proposed_text,
        confidence=payload.get("confidence"),
    )


def _load_obligation_payload(text: str, requirement_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DomainMapperDerivationError(
            f"obligation derivation response for requirement {requirement_id!r} was not "
            f"valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DomainMapperDerivationError(
            f"obligation derivation response for requirement {requirement_id!r} was not a "
            f"JSON object: {payload!r}"
        )
    return payload


def _resolve_proposed_obligation_text(
    payload: dict[str, Any], requirement_id: str, role_view_registry: dict[str, str]
) -> str:
    """Resolve a match-or-mint outcome to its proposed duty text, or raise
    the LEARNINGS.md B1-shaped `DomainMapperDerivationError` when neither
    a resolvable match nor a valid mint is present."""
    matched_existing_id = payload.get("matched_existing_id")
    if isinstance(matched_existing_id, str) and matched_existing_id in role_view_registry:
        return role_view_registry[matched_existing_id]

    new_text = payload.get("new_text")
    if isinstance(new_text, str) and new_text.strip():
        return new_text

    raise DomainMapperDerivationError(
        f"obligation derivation response for requirement {requirement_id!r} had neither a "
        f"matched_existing_id resolvable in the Role's own registry, nor a valid new_text, "
        f"nor unmatchable=true: {payload!r}"
    )


def _build_obligation_assignment(
    *,
    requirement_id: str,
    role_node_id: str,
    obligation_node_id: str | None,
    obligation_text: str | None,
    confidence: object,
) -> ObligationAssignment:
    try:
        return ObligationAssignment.model_validate(
            {
                "requirement_id": requirement_id,
                "role_node_id": role_node_id,
                "obligation_node_id": obligation_node_id,
                "obligation_text": obligation_text,
                "confidence": confidence,
            }
        )
    except ValidationError as exc:
        raise DomainMapperDerivationError(
            f"obligation derivation response for requirement {requirement_id!r} had an "
            f"invalid confidence value: {exc}"
        ) from exc


# --- Capability derivation (PLAN_REVIEWED.md §11 Increment 13) -------------
#
# Ported in spirit from `spikes/cellar2/derive_capabilities.py::_SYSTEM_PROMPT`,
# retaining its multi-capability-per-Obligation support unchanged — a
# proven finding from that spike (a single Obligation may bundle more than
# one distinct technical/organizational capacity, e.g. a reporting duty
# needing both "Incident Detection" and "Regulatory Notification
# Workflow") worth keeping, per PLAN_REVIEWED.md §7.4/§11 Increment 13's
# explicit instruction. Unlike obligation derivation's Role-scoped
# `role_view`, the registry shown here spans the WHOLE run across every
# distinct Obligation processed so far — Capability convergence is
# deliberately Obligation- and Role-independent (§7.4); there is no
# role-qualification concept anywhere in this stage.

CAPABILITY_DERIVATION_SYSTEM_PROMPT = """You maintain a canonical registry of Capabilities \
(reusable technical/organizational capacities) for a compliance graph, built as Obligations \
are processed in order. Given an Obligation's duty text and the registry built so far, decide \
which Capability/Capabilities it requires:

- For each capacity the Obligation genuinely requires, either reuse an existing Capability \
from the registry (if it names a real match) or mint a new one -- a short, generic capacity \
name (e.g. "Data Encryption", "Access Control System", "Security Logging"), not a paraphrase \
of the Obligation text itself.
- A single Obligation may require more than one distinct Capability if it bundles multiple \
technical/organizational capacities (e.g. a reporting duty needing both "Incident Detection" \
and "Regulatory Notification Workflow"). Do not, however, split one coherent capacity into \
near-duplicates just because the wording has multiple clauses -- only split when the \
capacities are genuinely distinct.
- On minting, also write a one-line description: Capability's name is deliberately terse, so \
the description is often the only thing telling a reader what it actually means without \
tracing every edge back to source text. A reused Capability's description is not regenerated.

confidence: your own certainty, 0.0-1.0, in each decision (reuse or mint). Always include it, \
per Capability.

Return strict JSON: {"capabilities": [{"matched_existing_id": str|null, "new_name": str|null, \
"new_description": str|null, "confidence": float}]}. For each item, exactly one of \
matched_existing_id/new_name must be non-null; new_description is set only alongside \
new_name."""


def parse_capability_response(
    text: str,
    obligation_node_id: str,
    registry: dict[str, tuple[str, str | None]],
) -> list[CapabilityDecision]:
    """Parse one Obligation's raw capability-derivation completion text
    into a list of `CapabilityDecision`s — ZERO OR MORE per Obligation
    (multi-capability-per-Obligation support, PLAN_REVIEWED.md §7.4).

    `registry` is the whole-run Capability registry (`capability_id ->
    (name, description)`, §7.4) built up so far across every distinct
    Obligation processed — used to resolve a `matched_existing_id` back
    into its name/description (the model is never asked to repeat text it
    already has access to).

    Each response item resolves to exactly one `CapabilityDecision`:
    - a `matched_existing_id` present in `registry` -> that entry's own
      name/description, `capability_node_id` equal to the matched id
      itself.
    - a non-empty `new_name` -> `capability_node_id =
      identity.capability_id(new_name)`, `description = new_description`
      (may be `None`).

    Raises `DomainMapperDerivationError`, naming `obligation_node_id`, on:
    - malformed JSON (`json.JSONDecodeError`) or a non-object response,
    - a response missing the top-level `"capabilities"` key, or whose
      value is not a list,
    - a non-object item in that list,
    - an item with neither a `matched_existing_id` resolvable in
      `registry` nor a valid `new_name` set — the same "surface, don't
      crash" defensive shape `parse_obligation_response` follows for its
      own LEARNINGS.md B1 case,
    - an invalid/missing `confidence` on any item.
    """
    items = _load_capability_items(text, obligation_node_id)
    return [
        _build_capability_decision(item, obligation_node_id, registry) for item in items
    ]


def _load_capability_items(text: str, obligation_node_id: str) -> list[Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DomainMapperDerivationError(
            f"capability derivation response for obligation {obligation_node_id!r} was not "
            f"valid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict) or "capabilities" not in payload:
        raise DomainMapperDerivationError(
            f"capability derivation response for obligation {obligation_node_id!r} is missing "
            f"the top-level 'capabilities' key: {payload!r}"
        )

    items: Any = payload["capabilities"]
    if not isinstance(items, list):
        raise DomainMapperDerivationError(
            f"capability derivation response for obligation {obligation_node_id!r} has a "
            f"non-list 'capabilities' value: {items!r}"
        )
    return items


def _build_capability_decision(
    item: Any,
    obligation_node_id: str,
    registry: dict[str, tuple[str, str | None]],
) -> CapabilityDecision:
    if not isinstance(item, dict):
        raise DomainMapperDerivationError(
            f"capability derivation response for obligation {obligation_node_id!r} has a "
            f"non-object capability item: {item!r}"
        )
    resolved_id, name, description = _resolve_capability(item, obligation_node_id, registry)
    try:
        return CapabilityDecision.model_validate(
            {
                "obligation_node_id": obligation_node_id,
                "capability_node_id": resolved_id,
                "name": name,
                "description": description,
                "confidence": item.get("confidence"),
            }
        )
    except ValidationError as exc:
        raise DomainMapperDerivationError(
            f"capability derivation response for obligation {obligation_node_id!r} had an "
            f"invalid capability item: {exc}"
        ) from exc


def _resolve_capability(
    item: dict[str, Any],
    obligation_node_id: str,
    registry: dict[str, tuple[str, str | None]],
) -> tuple[str, str, str | None]:
    """Resolve one response item to `(capability_node_id, name,
    description)`, or raise the LEARNINGS.md B1-shaped
    `DomainMapperDerivationError` when neither a resolvable match nor a
    valid mint is present."""
    matched_existing_id = item.get("matched_existing_id")
    if isinstance(matched_existing_id, str) and matched_existing_id in registry:
        name, description = registry[matched_existing_id]
        return matched_existing_id, name, description

    new_name = item.get("new_name")
    if isinstance(new_name, str) and new_name.strip():
        new_description = item.get("new_description")
        description = new_description if isinstance(new_description, str) else None
        return capability_id(new_name), new_name, description

    raise DomainMapperDerivationError(
        f"capability derivation response for obligation {obligation_node_id!r} had a "
        f"capability item with neither a matched_existing_id resolvable in the registry, nor "
        f"a valid new_name: {item!r}"
    )
