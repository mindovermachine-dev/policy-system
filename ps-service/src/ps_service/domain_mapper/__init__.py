"""ps_service.domain_mapper — package front door.

Re-exports `extract_roles_and_requirements`
(`ps_service.domain_mapper.extraction`) and
`derive_obligations_and_capabilities` (`ps_service.domain_mapper.derivation`),
the two public actions, per PLAN_REVIEWED.md §1's file-layout intent.

GH #76 removed this package's former third action outright -- the
internal-source-only LLM mint/match of Policy/Standard/Control that used to
live in this package (issue #54 S3) -- in favor of Policy/Standard/Control
being authored directly in the internal-seed intake document and
validated/minted/persisted by `ps_service.ingestion.adapters.internal_seed`,
a different package, not this one. This package's two remaining public
actions now write only Role/Requirement/Obligation/Capability.

Also exposes `DOMAIN_SCHEMA_VERSION`, the version tag for the domain-model
shape this package's public actions write. Bump this whenever a change to
this shape would make an already-exported curated artifact's replay produce
different graph content than a fresh extraction would. It is opaque outside
an equality check -- there is no ordering semantics to encode, only equality.

**GH #76 bumped it from `"1"` to `"2"`**: before this issue, a fresh
extraction/derivation run over an internal-source RegulatoryInstrument would
also mint Policy/Standard/Control nodes as a side effect of this package's
own actions (the now-removed third action described above). That no longer
happens -- this package's public actions write strictly less graph content
than they did under schema version `"1"`, so a version-`"1"`-tagged curated
artifact's replay is no longer equivalent to what a fresh run of this
package's current actions would produce.
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
