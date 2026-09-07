"""Pure identity functions for `ps_service.domain_mapper`.

The identity formulas from `docs/artifacts/ps-domain-concepts.md`.

`obligation_id(role_node_id, text)` is **Role-scoped**: the hash folds in
the bearing Role, exactly as `role_id` folds in its defining RegulatoryInstrument.
This is the resolution of issue #42 — an Obligation is a weak entity of
exactly one Role, so `Role -[:HAS]-> Obligation` `1 : 0..*` holds
structurally (two sources' duties can never collide onto one Obligation
node, because their Roles are always distinct nodes). Cross-source
convergence on the regulatory spine happens only at `Capability`, whose id
(`capability_id`) is deliberately name-only. An earlier revision of this
module made `obligation_id` duty-text-only to make Obligation canonical
across regulations; #42 retired that — see the issue and
`ps-domain-concepts.md`'s Obligation section for why.
"""

from __future__ import annotations

import hashlib
import re


def _slug(text: str) -> str:
    """Slugify `text`: lowercase, non-alphanumeric runs to `_`, no leading/trailing `_`."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return re.sub(r"_+", "_", slug)


def _hash(text: str) -> str:
    """First 6 hex chars of `text`'s SHA-1 digest.

    An opaque disambiguation suffix, not a security control.
    """
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]


def role_id(name: str, regulatory_instrument_id: str) -> str:
    """`role_{slug}_{hash}` — content-derived from `name` + the defining RegulatoryInstrument.

    Regulation-scoped (Role is NOT canonical across regulations, per
    `ps-domain-concepts.md`).
    """
    return f"role_{_slug(name)}_{_hash(f'{regulatory_instrument_id}:{name.lower()}')}"


def requirement_id(
    regulatory_instrument_id: str, article: str, paragraph: str, letter: str | None
) -> str:
    """`{REG}_req_art_{ARTICLE}.{PARAGRAPH}[LETTER]` — structurally fixed, non-opaque.

    Generated from the source location alone.
    """
    suffix = letter or ""
    return f"{regulatory_instrument_id}_req_art_{article}.{paragraph}{suffix}"


def obligation_id(role_node_id: str, text: str) -> str:
    """`obl_{slug}_{hash}` — content-derived from the duty statement AND the bearing Role's id.

    The Role it links to via `HAS`. Role-scoped (Obligation is a weak
    entity of exactly one Role, per `ps-domain-concepts.md` / issue #42).
    The `{slug}` is still the duty text alone, for human readability; the
    Role only enters the opaque `{hash}`, never the id string, mirroring
    `role_id`'s own shape.
    """
    return f"obl_{_slug(text)}_{_hash(f'{role_node_id}:{text.lower()}')}"


def capability_id(name: str) -> str:
    """`cap_{slug}_{hash}` — content-derived from `name` alone.

    Deliberately excludes any specific requiring Obligation, so identical
    capability names converge onto one node across Obligations/regulations.
    """
    return f"cap_{_slug(name)}_{_hash(name.lower())}"


def policy_id(title: str) -> str:
    """`pol_{slug}_{hash}` — content-derived from the Policy's own `title` alone.

    Deliberately NOT derived from any governing Capability (`ps-domain-
    concepts.md`'s Policy identity note: a single Policy commonly governs
    several Capabilities at once, so deriving from any one of them would be
    incoherent). Mirrors `capability_id`'s own "identity comes from what the
    node itself is" shape.
    """
    return f"pol_{_slug(title)}_{_hash(title.lower())}"


def standard_id(policy_node_id: str, version: str) -> str:
    """`std_{POLICY}_v{VERSION}` — derived from the Policy it supports plus version.

    A weak-entity id (like `requirement_id`/`obligation_id`'s own owning-
    parent composition), not a canonical hash: a Standard exists only in the
    context of exactly one Policy, so there is no cross-Policy reuse to
    protect against by opacity.
    """
    return f"std_{policy_node_id}_v{version}"


def control_id(standard_node_id: str, control_type: str) -> str:
    """`ctrl_{STANDARD}_{TYPE}` — derived from the Standard it verifies plus control type.

    Same weak-entity pattern as `standard_id`: a Control exists only to
    verify exactly one Standard, so there is no cross-Standard reuse to
    protect against.
    """
    return f"ctrl_{standard_node_id}_{control_type}"
