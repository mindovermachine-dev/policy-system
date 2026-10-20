"""Pure identity functions for `ps_service.domain_mapper`.

Implements the identity formulas from `docs/artifacts/ps-domain-concepts.md`. Every function is
deterministic and side-effect free: the same inputs always yield the same node id, which is
what lets re-runs `MERGE` onto existing nodes instead of duplicating them.

`role_id` hashes the name it is given. Extraction passes the NORMALISED name
(`role_names.normalise_role_name`: article dropped, last word singularised), so spellings of one
actor share one id.

`obligation_id(role_node_id, text)` is Role-scoped: the hash folds in the bearing Role, exactly
as `role_id` folds in its defining RegulatoryInstrument. An Obligation is a weak entity of
exactly one Role, so `Role -[:HAS]-> Obligation` is `1 : 0..*` structurally: two sources' duties
can never collide onto one Obligation node, because their Roles are always distinct nodes.
Cross-source convergence on the regulatory spine happens only at `Capability`, whose id
(`capability_id`) is deliberately name-only. See the Obligation section of
`ps-domain-concepts.md`.
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
    """Return the Role id `role_{slug}_{hash}`, scoped to its defining RegulatoryInstrument.

    Role is not canonical across regulations (see `ps-domain-concepts.md`): the instrument id
    enters only the opaque `{hash}`, so the same name under two instruments gives two ids.

    Args:
        name: The Role's name; case-insensitive for the hash, slugified for the readable part.
        regulatory_instrument_id: Id of the RegulatoryInstrument that defines the Role.

    Returns:
        The content-derived Role id.
    """
    return f"role_{_slug(name)}_{_hash(f'{regulatory_instrument_id}:{name.lower()}')}"


def requirement_id(
    regulatory_instrument_id: str, article: str, paragraph: str, letter: str | None
) -> str:
    """Return the Requirement id `{REG}_req_art_{ARTICLE}.{PARAGRAPH}[LETTER]`.

    The id is structurally fixed and non-opaque, generated from the source location alone.

    Args:
        regulatory_instrument_id: Id of the RegulatoryInstrument the Requirement belongs to.
        article: Article number of the source unit.
        paragraph: Paragraph number of the source unit (`"1"` for a paragraph-less Article).
        letter: Sub-paragraph letter, or `None` (or empty) when the unit holds one duty.

    Returns:
        The location-derived Requirement id.
    """
    suffix = letter or ""
    return f"{regulatory_instrument_id}_req_art_{article}.{paragraph}{suffix}"


def obligation_id(role_node_id: str, text: str) -> str:
    """Return the Obligation id `obl_{slug}_{hash}`, scoped to the Role that bears the duty.

    The `{slug}` is the duty text alone, for readability; the Role enters only the opaque
    `{hash}`, never the id string, mirroring `role_id`'s shape. An Obligation is a weak entity of
    exactly one Role (see `ps-domain-concepts.md`).

    Args:
        role_node_id: Id of the Role the Obligation links to via `HAS`.
        text: The duty statement; case-insensitive for the hash.

    Returns:
        The content-derived Obligation id.
    """
    return f"obl_{_slug(text)}_{_hash(f'{role_node_id}:{text.lower()}')}"


def capability_id(name: str) -> str:
    """Return the Capability id `cap_{slug}_{hash}`, derived from `name` alone.

    The id excludes any requiring Obligation, so identical capability names converge onto one
    node across Obligations and regulations.

    Args:
        name: The Capability name; case-insensitive for the hash.

    Returns:
        The name-derived Capability id.
    """
    return f"cap_{_slug(name)}_{_hash(name.lower())}"


def policy_id(title: str, prior_policy_id: str | None = None) -> str:
    """Return the Policy id `pol_{slug}_{hash}`, derived from the Policy's own `title`.

    The id is not derived from any governing Capability: one Policy commonly governs several
    Capabilities at once, so deriving from any one of them would be incoherent (see the Policy
    identity note in `ps-domain-concepts.md`). With no `prior_policy_id` (the first version of a
    Policy) identity is canonical and title-only.

    A given `prior_policy_id` marks a successor version: it enters the opaque `{hash}` only,
    never the visible `{slug}`, following the "parent enters the hash, never the slug" pattern
    of `standard_id` and `control_id`. Identity is therefore weak-entity-shaped for a successor
    version and canonical for the first.

    Args:
        title: The Policy title; case-insensitive for the hash.
        prior_policy_id: Id of the Policy version this one succeeds, or `None` for a first
            version.

    Returns:
        The content-derived Policy id.
    """
    if prior_policy_id is None:
        return f"pol_{_slug(title)}_{_hash(title.lower())}"
    return f"pol_{_slug(title)}_{_hash(f'{prior_policy_id}:{title.lower()}')}"


def practice_area_id(name: str) -> str:
    """Return the PracticeArea id `pa_{slug}_{hash}`, derived from `name` alone.

    Canonical and name-only, like `capability_id` and `policy_id` (see the PracticeArea section
    of `ps-domain-concepts.md`): never derived from a covered Capability or owned Policy, so
    identical PracticeArea names converge onto one node regardless of what they connect to.

    Args:
        name: The PracticeArea name; case-insensitive for the hash.

    Returns:
        The name-derived PracticeArea id.
    """
    return f"pa_{_slug(name)}_{_hash(name.lower())}"


def standard_id(policy_node_id: str, title: str) -> str:
    """Return the Standard id `std_{slug}_{hash}`, from the Standard's `title` and its Policy.

    `SUPPORTED_BY` is `1 : 1..*`, so a Policy may have several Standards. Keying identity on the
    Standard's own `title` keeps two Standards under one Policy distinct: the `{slug}` is the
    child's title for readability, and the parent `policy_node_id` enters only the opaque
    `{hash}`. `version` is an ordinary node property, not an identity input.

    Args:
        policy_node_id: Id of the Policy the Standard links to via `SUPPORTED_BY`.
        title: The Standard title; case-insensitive for the hash.

    Returns:
        The content-derived Standard id.
    """
    return f"std_{_slug(title)}_{_hash(f'{policy_node_id}:{title.lower()}')}"


def control_id(standard_node_id: str, title: str) -> str:
    """Return the Control id `ctrl_{slug}_{hash}`, from the Control's `title` and its Standard.

    `IMPLEMENTED_BY` is `1 : 0..*`, so a Standard may have several Controls, even of the same
    `type`. Keying identity on the Control's own `title` keeps them distinct: the `{slug}` is the
    child's title for readability, and the parent `standard_node_id` enters only the opaque
    `{hash}`. `type` is an ordinary node property, not an identity input.

    Args:
        standard_node_id: Id of the Standard the Control links to via `IMPLEMENTED_BY`.
        title: The Control title; case-insensitive for the hash.

    Returns:
        The content-derived Control id.
    """
    return f"ctrl_{_slug(title)}_{_hash(f'{standard_node_id}:{title.lower()}')}"


def risk_path_id(name: str) -> str:
    """Return the RiskPath id `rp_{slug}_{hash}`, derived from `name` alone.

    Canonical and name-only, like `capability_id`, `policy_id` and `practice_area_id` (see the
    RiskPath section of `ps-domain-concepts.md`).

    Args:
        name: The RiskPath name; case-insensitive for the hash.

    Returns:
        The name-derived RiskPath id.
    """
    return f"rp_{_slug(name)}_{_hash(name.lower())}"
