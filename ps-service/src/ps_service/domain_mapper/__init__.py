"""Domain Mapper: turns a regulation's native structural graph into its baseline domain graph.

Re-exports the two public actions:

- `extract_roles_and_requirements` (`ps_service.domain_mapper.extraction`) reads the native
  graph and writes Role and Requirement nodes.
- `derive_obligations_and_capabilities` (`ps_service.domain_mapper.derivation`) derives
  Obligation and Capability nodes from the Requirements.

Policy, Standard and Control nodes are not written here: they are authored in the
internal-seed intake document and persisted by `ps_service.ingestion.adapters.internal_seed`.

Also exposes `DOMAIN_SCHEMA_VERSION`, the version tag for the shape of the domain model these
actions write. It is opaque outside an equality check; there is no ordering to encode. It must
change whenever a change to that shape would make the replay of an already-exported curated
artifact produce different graph content than a fresh extraction would.
"""

from __future__ import annotations

from ps_service.domain_mapper.derivation import derive_obligations_and_capabilities
from ps_service.domain_mapper.extraction import extract_roles_and_requirements

DOMAIN_SCHEMA_VERSION = "2"

__all__ = [
    "DOMAIN_SCHEMA_VERSION",
    "derive_obligations_and_capabilities",
    "extract_roles_and_requirements",
]
