#!/usr/bin/env python3
"""Seed one synthetic row in each state table a backup and restore round trip must preserve.

Run inside the ps-service pod (it reads the service's own configuration from the environment):

    kubectl exec -i deploy/policy-system-ps-service -- python - < seed_state.py

Rows go through the service's own stores: one `runtime_config` row (the curated-source base URL),
one finished `ingestion_runs` row, one `pending_approvals` row and one `signing_credentials` row.
Every value is synthetic except the base URL, which is the service's own configured catalog URL so a
later seed or restore still reaches the catalog. It is only meant for the throwaway kind cluster
that `require_allowed_context` restricts the verifier to. Nothing here contacts the graph database.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

import ps_service.curated_source  # noqa: F401  # pyright: ignore[reportUnusedImport] -- registers the curated-source runtime-config key
from ps_service.audit.store import PsycopgAuditStore
from ps_service.config import load_config
from ps_service.curated_source.store import get_override, set_override
from ps_service.ingestion_runs.store import PsycopgIngestionRunStore
from ps_service.passkey_signing.signing_credential_store import PsycopgSigningCredentialStore
from ps_service.passkey_signing.store import PsycopgPendingApprovalStore
from ps_service.runtime_config import PsycopgRuntimeConfigStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ps_service.ingestion_runs.store import IngestionRunStore
    from ps_service.passkey_signing.signing_credential_store import SigningCredentialStore
    from ps_service.passkey_signing.store import PendingApprovalStore
    from ps_service.runtime_config import RuntimeConfigStore

SEED_ACTOR_SUBJECT = "system:backup-restore-verify"
SEED_RUN_ID = "00000000-0000-4000-8000-000000284001"
SEED_CELEX = "32099R0000"
SEED_SHORT_NAME = "BRV"
SEED_TOOL_NAME = "backup-restore-verify"
SEED_CREDENTIAL_ID = b"backup-restore-verify-credential"
SEED_PUBLIC_KEY = b"backup-restore-verify-public-key"
SEED_RP_ID = "backup-restore-verify.invalid"


def seed_state(
    runtime_config_store: RuntimeConfigStore,
    run_store: IngestionRunStore,
    approval_store: PendingApprovalStore,
    credential_store: SigningCredentialStore,
    *,
    issuer: str,
    catalog_url: str,
) -> None:
    """Write one synthetic row to each of the four state tables.

    The runtime-config row, the ingestion run and the signing credential are written only when
    absent, so a repeated call leaves them as they are. The pending-approval store has no lookup
    by tool, so every call adds one more pending approval.

    Args:
        runtime_config_store: Store holding the curated-source base-URL override.
        run_store: Store of ingestion runs.
        approval_store: Store of pending passkey approvals.
        credential_store: Store of enrolled signing credentials.
        issuer: Identity issuer recorded for the synthetic actor on every row.
        catalog_url: The service's effective curated-source base URL, stored as the override so
            later restores from the catalog keep resolving the same source.

    Raises:
        Exception: Whatever a store raises on a failed read or write; rows already written stay.

    Side Effects:
        Inserts rows in PS state Postgres and in the passkey-signing Postgres, plus the audit
        events the stores write with them.
    """
    actor = (SEED_ACTOR_SUBJECT, issuer)
    if get_override(runtime_config_store) is None:
        set_override(runtime_config_store, catalog_url, actor=actor)
    if run_store.get_run(SEED_RUN_ID) is None:
        run_store.create_run(
            run_id=SEED_RUN_ID, celex=SEED_CELEX, short_name=SEED_SHORT_NAME, actor=actor
        )
        run_store.complete_run(
            SEED_RUN_ID, status="succeeded", result={"celex": SEED_CELEX}, error=None
        )
    approval_store.create_pending_approval(
        tool_name=SEED_TOOL_NAME,
        normalized_args={"celex": SEED_CELEX},
        actor_subject=actor[0],
        actor_issuer=actor[1],
        display_summary={"summary": "synthetic approval"},
    )
    if credential_store.get_by_credential_id(SEED_CREDENTIAL_ID) is None:
        credential_store.create_signing_credential(
            actor_subject=actor[0],
            actor_issuer=actor[1],
            credential_id=SEED_CREDENTIAL_ID,
            public_key=SEED_PUBLIC_KEY,
            sign_count=0,
            rp_id=SEED_RP_ID,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Seed the synthetic state rows using the service's own configuration.

    Args:
        argv: Command-line arguments without the program name; `None` reads `sys.argv`.

    Returns:
        0 once every row is recorded.

    Raises:
        Exception: Any configuration or store failure propagates unchanged.

    Side Effects:
        Inserts rows in the state tables and logs one event line to stderr.
    """
    parser = argparse.ArgumentParser(
        description="Seed one synthetic row in each state table through the stores"
    )
    parser.parse_args(argv)
    config = load_config()
    issuer = config.authz_bootstrap_owner_issuer or "https://backup-restore-verify.invalid"
    audit_store = PsycopgAuditStore(config)
    seed_state(
        PsycopgRuntimeConfigStore(config, audit_store=audit_store),
        PsycopgIngestionRunStore(config, audit_store=audit_store),
        PsycopgPendingApprovalStore(config),
        PsycopgSigningCredentialStore(config),
        issuer=issuer,
        catalog_url=config.curated_source_base_url,
    )
    sys.stderr.write(
        "event=seed_state tables=runtime_config,ingestion_runs,"
        "pending_approvals,signing_credentials\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
