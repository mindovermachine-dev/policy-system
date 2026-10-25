"""Which backup-restore seed tool fills each table the migrations create.

The seed tools in `tools/backup-restore-verify` write through the service's own stores and the
graph gateway, so their code never names a table. This declaration maps each
`<database>.<schema>.<table>` a migration creates to the seed tool whose run leaves it non-empty.
The verifier table guard (`test_verifier_tables.py`) requires every created table here or on
`SEED_EMPTY_EXCEPTIONS` in `scripts/verify-backup-restore.sh`, and every tool named here to exist
and be run by the verifier. Whether each table really holds a row after seeding is proven by the
verifier's live round trip, not by this file.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

SEEDED_TABLES: Mapping[str, str] = MappingProxyType(
    {
        # The curated restore writes its mutations through the graph gateway's log.
        "ps_state.graph_log.payloads": "seed_content.py",
        "ps_state.graph_log.groups": "seed_content.py",
        "ps_state.graph_log.entries": "seed_content.py",
        # Filled by the gateway when it checkpoints and marks the restored groups as applied.
        "ps_state.graph_log.checkpoints": "seed_content.py",
        "ps_state.graph_log.applied_markers": "seed_content.py",
        # Each grant writes an audit event; the other seed tools add more.
        "ps_state.audit_log.audit_events": "seed_grants.py",
        # Filled by the hash-chain trigger on each audit event insert.
        "ps_state.audit_log.audit_chain_head": "seed_grants.py",
        "ps_state.public.access_role_assignments": "seed_grants.py",
        "ps_state.public.runtime_config": "seed_state.py",
        "ps_state.public.ingestion_runs": "seed_state.py",
        "ps_signing.public.pending_approvals": "seed_state.py",
        "ps_signing.public.signing_credentials": "seed_state.py",
    }
)
"""Table to the seed tool file name (in `tools/backup-restore-verify`) that fills it."""
