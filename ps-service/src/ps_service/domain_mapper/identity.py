"""Pure identity functions for `ps_service.domain_mapper` — the identity
formulas from `docs/artifacts/ps-domain-concepts.md`.

`obligation_id(role_node_id, text)` is **Role-scoped**: the hash folds in
the bearing Role, exactly as `role_id` folds in its defining Regulation.
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
    """Lowercase, non-alphanumeric runs collapsed to a single `_`, no
    leading/trailing `_`."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return re.sub(r"_+", "_", slug)


def _hash(text: str) -> str:
    """First 6 hex characters of the SHA-1 digest of `text` — an opaque
    disambiguation suffix, not a security control."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


def role_id(name: str, regulation_id: str) -> str:
    """`role_{slug}_{hash}` — content-derived from `name` + the defining
    Regulation, regulation-scoped (Role is NOT canonical across
    regulations, per `ps-domain-concepts.md`)."""
    return f"role_{_slug(name)}_{_hash(f'{regulation_id}:{name.lower()}')}"


def requirement_id(regulation_id: str, article: str, paragraph: str, letter: str | None) -> str:
    """`{REG}_req_art_{ARTICLE}.{PARAGRAPH}[LETTER]` — structurally fixed,
    non-opaque, generated from the source location alone."""
    suffix = letter or ""
    return f"{regulation_id}_req_art_{article}.{paragraph}{suffix}"


def obligation_id(role_node_id: str, text: str) -> str:
    """`obl_{slug}_{hash}` — content-derived from the duty statement AND
    the id of the Role that bears it (the Role it links to via `HAS`),
    Role-scoped (Obligation is a weak entity of exactly one Role, per
    `ps-domain-concepts.md` / issue #42). The `{slug}` is still the duty
    text alone, for human readability; the Role only enters the opaque
    `{hash}`, never the id string, mirroring `role_id`'s own shape."""
    return f"obl_{_slug(text)}_{_hash(f'{role_node_id}:{text.lower()}')}"


def capability_id(name: str) -> str:
    """`cap_{slug}_{hash}` — content-derived from `name` alone,
    deliberately excluding any specific requiring Obligation, so identical
    capability names converge onto one node across Obligations/
    regulations."""
    return f"cap_{_slug(name)}_{_hash(name.lower())}"
