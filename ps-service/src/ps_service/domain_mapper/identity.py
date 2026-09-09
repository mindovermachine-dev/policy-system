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


def standard_id(policy_node_id: str, title: str) -> str:
    """`std_{slug}_{hash}` — content-derived from the Standard's `title` and Policy.

    Derived from the Standard's own `title` AND the Policy it supports (the
    Policy it links to via `SUPPORTED_BY`).

    GH #76 (AC-BI-002) fixed this from the earlier `std_{POLICY}_v{VERSION}`
    formula, which keyed identity on `version` — a property that is
    constant (`"1"`) at mint time and, more fundamentally, is not what
    distinguishes Standards under one Policy. `SUPPORTED_BY` is `1 : 1..*`
    (a Policy may have several Standards, e.g. one covering logging, one
    covering access control), so two distinct Standards under the same
    Policy that happened to share a `version` collided onto one node via
    `MERGE`. Keying on the Standard's own `title` instead — mirroring
    `obligation_id(role_node_id, text)`'s existing shape exactly — fixes
    this structurally: the `{slug}` is the child's own title, for human
    readability (two different titles under the same Policy are guaranteed
    two different ids by construction, since `_slug` differs whenever
    `title.lower()` differs); the parent `policy_node_id` enters only the
    opaque `{hash}`, never printed literally in the id string. `version`
    remains an ordinary node *property* — it simply stops being an identity
    input.
    """
    return f"std_{_slug(title)}_{_hash(f'{policy_node_id}:{title.lower()}')}"


def control_id(standard_node_id: str, title: str) -> str:
    """`ctrl_{slug}_{hash}` — content-derived from the Control's `title` and Standard.

    Derived from the Control's own `title` AND the Standard it verifies (the
    Standard it links to via `IMPLEMENTED_BY`).

    GH #76 (AC-BI-003) fixed this from the earlier `ctrl_{STANDARD}_{TYPE}`
    formula, which keyed identity on `type` — a 2-valued enum (`automated`/
    `manual`), not what distinguishes Controls under one Standard.
    `IMPLEMENTED_BY` is `1 : 0..*` (a Standard may have several Controls,
    e.g. two automated checks verifying different aspects of the same
    Standard), so two distinct Controls under the same Standard that
    happened to share a `type` collided onto one node via `MERGE` — this is
    AC-BI-003's literal "even of the same `type`" wording. Keying on the
    Control's own `title` instead — mirroring `standard_id(policy_node_id,
    title)`'s existing shape exactly, itself mirroring `obligation_id
    (role_node_id, text)` — fixes this structurally: the `{slug}` is the
    child's own title, for human readability (two different titles under
    the same Standard are guaranteed two different ids by construction,
    since `_slug` differs whenever `title.lower()` differs); the parent
    `standard_node_id` enters only the opaque `{hash}`, never printed
    literally in the id string. `type` remains an ordinary node *property*
    — it simply stops being an identity input.
    """
    return f"ctrl_{_slug(title)}_{_hash(f'{standard_node_id}:{title.lower()}')}"
