#!/usr/bin/env python3
r"""Seed graph content so a backup and restore round trip has data to verify.

Run inside the ps-service pod (it uses the service's own configuration, gateway and
curated-content source from the environment):

    kubectl exec -i deploy/policy-system-ps-service -- python - --instrument ENGPRAC-1.0 \\
        --instrument CRA-1.0 < seed_content.py

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
DEFAULT_INSTRUMENTS = ("ENGPRAC-1.0", "CRA-1.0")


def seed_instruments(
    instrument_ids: Sequence[str], restore_one: Callable[[str], object]
) -> list[str]:
    """Restore each instrument through the supplied restore function.

    Args:
        instrument_ids: Curated-catalog instrument ids, restored in the order given.
        restore_one: Restores a single instrument id; its return value is ignored.

    Returns:
        The ids that were restored, in order.

    Raises:
        Exception: Whatever `restore_one` raises for an instrument; ids after it are not restored.
    """
    restored: list[str] = []
    for instrument_id in instrument_ids:
        restore_one(instrument_id)
        restored.append(instrument_id)
    return restored


def main(argv: Sequence[str] | None = None) -> int:
    """Restore curated instruments into the live graphs through the service's own restore path.

    Args:
        argv: Command-line arguments without the program name; `None` reads `sys.argv`.
            `--instrument` (repeatable) selects the instrument ids, defaulting to
            `DEFAULT_INSTRUMENTS`.

    Returns:
        0 once every instrument is restored.

    Raises:
        Exception: Any configuration, gateway or restore failure propagates unchanged.

    Side Effects:
        Writes graph nodes through the Graph Write Gateway, records audit rows, appends
        service logs to a file in the system temp directory and logs progress events to stderr.
        The gateway is uninstalled and its reconciler stopped before returning.
    """
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
