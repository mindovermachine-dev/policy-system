"""ps_service.policy_lifecycle -- package front door (issue #134).

Policy/Standard/Control proposal lifecycle (draft -> proposed -> approved ->
deprecated). Domain path: `ps.service.policylifecycle`
(`docs/architecture/ps-service-container-architecture.md`).

Empty skeleton (Slice 2, PLAN.md §0.3/§3 S2) -- re-exports are added as this
component's public surface (rules, service, errors, audit actions,
graph_writer) lands in later slices.
"""

from __future__ import annotations
