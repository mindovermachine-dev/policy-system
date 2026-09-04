"""ps_service.domain_mapper — package front door.

Re-exports `extract_roles_and_requirements`
(`ps_service.domain_mapper.extraction`) and
`derive_obligations_and_capabilities` (`ps_service.domain_mapper.derivation`),
the two public actions, per PLAN_REVIEWED.md §1's file-layout intent.

Also exposes `DOMAIN_SCHEMA_VERSION`, the version tag for the domain-model
shape (Role/Requirement/Obligation/Capability, and, for internal sources,
Policy/Standard/Control -- see `docs/artifacts/ps-domain-concepts.md`) that
this package's public actions write. Bump this whenever a change to
ps-domain-concepts.md's node/edge shape would make an already-exported
curated artifact's replay produce different graph content than a fresh
extraction would. It is opaque outside an equality check -- there is no
ordering semantics to encode, only equality.
"""

from __future__ import annotations

from ps_service.domain_mapper.derivation import derive_obligations_and_capabilities
from ps_service.domain_mapper.extraction import extract_roles_and_requirements

DOMAIN_SCHEMA_VERSION = "1"

__all__ = [
    "DOMAIN_SCHEMA_VERSION",
    "derive_obligations_and_capabilities",
    "extract_roles_and_requirements",
]
