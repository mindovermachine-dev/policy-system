#!/usr/bin/env python3
r"""Seed graph content for the backup and restore verification (issue #216).

Run inside the ps-service pod (it uses the service's own configuration, gateway and
curated-content source from the environment):

    kubectl exec -i deploy/policy-system-ps-service -- python - --instrument ENGPRAC-1.0 \\
        --instrument DORA-1.0 < seed_content.py

Each instrument is restored from the curated-content catalog by the same orchestration entry point
`POST /restorations/from-catalog` calls (no LLM involved). ENGPRAC-1.0 imports draft Policy,
Standard and Control nodes; every restore writes through the Graph Write Gateway and records its
`instrument.restore` audit rows.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from ps_service.api.graph_write_access import install_graph_write_gateway
from ps_service.api.models import CatalogRestorationRequest
from ps_service.api.restore_orchestration import (
    build_default_restore_from_catalog_dependencies,
    run_restoration_from_catalog_source,
)
from ps_service.audit.emit import AuditContext
from ps_service.audit.store import PsycopgAuditStore
from ps_service.config import load_config
from ps_service.graph_gateway.default_gateway import build_default_graph_write_gateway
from ps_service.logging.facade import configure as configure_logging

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

SEED_OWNER_SUBJECT = "backup-restore-verify-owner"
DEFAULT_INSTRUMENTS = ("ENGPRAC-1.0", "DORA-1.0")


def seed_instruments(
    instrument_ids: Sequence[str], restore_one: Callable[[str], object]
) -> list[str]:
    """Restore each instrument through `restore_one`; return the ids restored, in order."""
    restored: list[str] = []
    for instrument_id in instrument_ids:
        restore_one(instrument_id)
        restored.append(instrument_id)
    return restored


def main(argv: Sequence[str] | None = None) -> int:
    """Restore the named curated instruments into the live graphs; return the exit code."""
    parser = argparse.ArgumentParser(description="Seed curated content through the restore path")
    parser.add_argument("--instrument", action="append", default=[])
    args = parser.parse_args(argv)
    configure_logging(
        Path(tempfile.gettempdir()) / "ps-seed-content.jsonl", mirror_failures_to_stderr=True
    )
    config = load_config()
    issuer = config.authz_bootstrap_owner_issuer or "https://backup-restore-verify.invalid"
    owner = (SEED_OWNER_SUBJECT, issuer)
    dependencies = build_default_restore_from_catalog_dependencies()
    audit = AuditContext(owner, PsycopgAuditStore(config))

    def restore_one(instrument_id: str) -> object:
        sys.stderr.write(f"event=seed_restore_start instrument_id={instrument_id}\n")
        response = run_restoration_from_catalog_source(
            CatalogRestorationRequest(instrument_id=instrument_id),
            config=config,
            actor="backup-restore-verify",
            dependencies=dependencies,
            audit=audit,
            owner=owner,
        )
        sys.stderr.write(f"event=seed_restore_done instrument_id={instrument_id}\n")
        return response

    gateway = build_default_graph_write_gateway(config)
    install_graph_write_gateway(gateway)
    try:
        seed_instruments(args.instrument or list(DEFAULT_INSTRUMENTS), restore_one)
    finally:
        install_graph_write_gateway(None)
        gateway.stop_reconciler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
