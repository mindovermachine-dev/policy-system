"""ps_service.domain_mapper — package front door.

Re-exports `extract_roles_and_requirements`
(`ps_service.domain_mapper.extraction`) and
`derive_obligations_and_capabilities` (`ps_service.domain_mapper.derivation`),
the two public actions, per PLAN_REVIEWED.md §1's file-layout intent.
"""

from __future__ import annotations

from ps_service.domain_mapper.derivation import derive_obligations_and_capabilities
from ps_service.domain_mapper.extraction import extract_roles_and_requirements

__all__ = [
    "derive_obligations_and_capabilities",
    "extract_roles_and_requirements",
]
