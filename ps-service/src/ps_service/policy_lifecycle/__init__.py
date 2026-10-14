"""Policy lifecycle component: Policy/Standard/Control governance for the compliance graph.

Owns the proposal lifecycle of a Policy and its Standards and Controls (draft -> proposed ->
approved -> deprecated), the rules that gate each transition, the typed audit details recorded for
it, and the graph reads and gateway writes behind it. Domain path: `ps.service.policylifecycle`
(`docs/architecture/ps-service-container-architecture.md`).

The package re-exports nothing; callers import the submodules they need (`service`, `rules`,
`errors`, `audit_actions`, `graph_writer`, `graph_submission`).
"""

from __future__ import annotations
