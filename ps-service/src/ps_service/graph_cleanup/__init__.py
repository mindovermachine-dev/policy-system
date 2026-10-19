"""Compliance Officer graph cleanup of the single-tenant compliance graph.

Cleans up duplicates the ingestion pipeline leaves in the graph. Every change is decided by a
human: the component discovers near-duplicate Capabilities and duplicate Obligations, previews
a merge, then (after an approval) merges Capabilities, merges Obligations, releases a
Capability from a draft governing policy, or reverses an earlier merge. Domain path:
`ps.service.graphcleanup`.

The component is separate from Company Merge, which only adds and merges during the pipeline
(`docs/architecture/ps-service-container-architecture.md`, Company Merge). Company Merge
follows the `Merged<Kind>` markers this component writes but never creates them.
"""

from __future__ import annotations
