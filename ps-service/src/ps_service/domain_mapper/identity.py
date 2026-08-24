"""Pure identity functions for `ps_service.domain_mapper` — the canonical
id formulas from `docs/artifacts/ps-domain-concepts.md` (PLAN_REVIEWED.md
§0.2/§3), ported from `spikes/cellar2/schema.py`'s `_slug`/`_hash` shape.

`obligation_id(text)` is duty-text-only, deliberately dropping the
`role_node_id` parameter `spikes/cellar2/schema.py::obligation_id` bakes in
— this is PLAN_REVIEWED.md §3.1's B1 fix. The doc's own Company Merge >
Obligation > Constraints table states the identity is "derived from duty
statement only — enables reuse across regulations"; baking `role_node_id`
into the hash would make Obligation transitively regulation-scoped (since
Role identity already is), defeating that convergence property. The `HAS`
1:1-with-Role cardinality this creates tension with is preserved instead at
the derivation-time write layer (`derivation.py`'s whole-run registry,
PLAN_REVIEWED.md §7.3), not by polluting this hash — see §3.1 for the full
resolution. Do not add a `role_node_id` parameter here.
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


def obligation_id(text: str) -> str:
    """`obl_{slug}_{hash}` — content-derived from the duty statement
    ALONE. Deliberately regulation- and role-independent: no other
    parameter is accepted. See the module docstring and PLAN_REVIEWED.md
    §3.1 for why this is not `obligation_id(role_node_id, text)`."""
    return f"obl_{_slug(text)}_{_hash(text.lower())}"


def capability_id(name: str) -> str:
    """`cap_{slug}_{hash}` — content-derived from `name` alone,
    deliberately excluding any specific requiring Obligation, so identical
    capability names converge onto one node across Obligations/
    regulations."""
    return f"cap_{_slug(name)}_{_hash(name.lower())}"
